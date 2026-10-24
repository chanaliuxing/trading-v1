内存增量聚合**，同时内置**方案 A 作为自动回退**（当聚合器数据不够或异常时，临时只用 1s/5s + VIX + 仓位做判断）。理由：B 能给 LLM 完整的多周期画面（1s→5s→1m→5m→15m），不依赖 ClickHouse 的 features 实时性；冷启动也可用“预填充”解决。

# 选择：方案 B（带 A 级回退）

* **主路径**：Redis 1s → 内存增量聚合出 5s/1m/5m/15m（并计算核心特征：VWAP、EMA、RSI、量能/相对量、VWAP 偏离、zscore 等）。
* **回退**：若某周期样本不足（比如刚开盘前几分钟），Prompt 中**显式标注缺失**并降级决策（限制动作为 WAIT/REDUCE 或降低仓位）；必要时临时退回方案 A（仅 1s/5s + VIX + 仓位）。

# 落地步骤（不写代码版）

1. **新增 RealtimeAggregator**

   * 结构：每个 symbol 维护 `deque`（1s:60、5s:12、1m:10、5m:6、15m:4），边界对齐（整 5s/1m/5m/15m）滚动聚合。
   * 指标：OHLC/VWAP/Volume/Trades + 轻量特征（EMA9/21、RSI14、VWAP 偏离 bps、rvol、zscore、近 N 根涨跌占比等）。
   * 输出：`get_snapshot(symbol)` 返回 `{"1s":[…], "5s":[…], "1m":[…], "5m":[…], "15m":[…], "computed":{"rvol_5m":…}}`。

2. **冷启动预填充（可选强烈推荐）**

   * 开盘前/启动时，从 **sfs.sfs_5s**（或 `trading.bars_5s_current`）取最近若干分钟，填充 5s→再聚合到 1m/5m/15m。
   * 这样一启动就有中周期背景，避免“15 分钟后才有 15m”的盲区。

3. **集成到现有数据通路**

   * `run_single_bot.py` 的每个 cycle：

     * 读取 Redis 1s 最新 bar → `aggregator.update_from_1s(symbol, bar_1s)`
     * `mtf = aggregator.get_snapshot(symbol)`
     * Prompt 构建优先用 `mtf`；若某层周期不足，Prompt 中**注记缺失**并触发**置信封顶与动作降级**（例如空仓禁止 BUY，持仓允许 REDUCE/EXIT）。
   * 保留原来的 ClickHouse 访问仅用于：VIX/大盘（SPY/QQQ）/日级 ATR 等较慢维度。

4. **Prompt 侧约束（已经有 R1 模版可直接套）**

   * 明确指令：如果 `missing_timeframes` 非空 → “不得开新仓”，仅可 WAIT/REDUCE/EXIT；若仍要 BUY 必须返回 `force_reason` 且 `risk_units <= 1`。
   * 供数表中加入：每个周期的**样本条数**、**最后时间戳**、**是否齐整**（bar boundary aligned）。

5. **交易决策与仓位映射**

   * LLM 仅输出动作 + 风险单位（0–3）；主程序用**建议止损距离**+**风险预算 R**换算成 shares（你现有管道已支持）。
   * 若降级/缺数 → 直接把 `risk_units = 0 or 1`（封顶），防误冲。

6. **健康检查与自动回退**

   * 每周期打印：`agg_status(symbol) = { '1s':60/ok,'5s':12/ok,'1m':8/need,'5m':1/need,'15m':0/missing }`
   * 触发条件：若 1m 或 5m 持续缺数 N 个 cycle，则自动切回方案 A（并在记忆中记录“降级原因”）。

7. **日志与审计**

   * 记录：输入多周期摘要哈希、缺数标志、LLM 决策、动作降级原因、shares 换算细节、eod/禁止开仓等时间规则触发情况。
   * 方便你定位“为什么那一刻不让开仓/为什么仓位被缩小”。

# 接口/参数建议

* `--use-aggregator true`（默认开）
* `--prefill-5s-mins 20`（预填充多少分钟 5s 数据）
* `--min-bars-1m 10 --min-bars-5m 3 --min-bars-15m 1`（各周期最小样本）
* `--degrade-if-missing true`（缺数自动降级）
* `--fallback-to-planA true`（聚合器异常时使用方案 A）
* 日内时间管理参数与这套兼容（之前你已加 `--close-at/--close-before/--no-new-positions-mins`）

# 验收标准（建议）

* 开盘后 2–3 分钟内：LLM 只能 WAIT/REDUCE（因为 1m/5m 样本不足），日志显示“降级：缺少 1m/5m”。
* 聚合 10 分钟后：开始允许小仓（risk_units ≤ 1），并在波动增大/一致性提升时逐步放大。
* 收盘前 5 分钟：即使聚合完整，也**不允许 BUY**（遵守“禁止新开仓”时间窗）。
* 聚合器异常时：自动退回方案 A，决策仍可运行；恢复后回到 B，并在记忆/日志中记录切换。

---



太好了，那我把**“内存增量聚合 + 缺数降级 + Prompt 供数”**一次性落地到你现有的 Claude 基座里。下面给你**可直接复制**的完整文件与最小改动的补丁。按顺序粘贴即可。

---

# ① 新增文件：`realtime_aggregator.py`

> 从 Redis 的 **1s bar** 动态滚动聚合出 **5s / 1m / 5m / 15m**；并提供一个快照接口给 Prompt 构建使用。支持启动时用 5s 历史做**预填充**（把 5s 粗略“展开”为 1s 以快速形成中周期背景）。

```python
# realtime_aggregator.py
from __future__ import annotations
import datetime as dt
from collections import deque, defaultdict
from typing import Dict, List, Optional, Tuple, Any

UTC = dt.timezone.utc

def _to_dt_utc(ts: Any) -> dt.datetime:
    """Accept int/float(epoch seconds), str(iso), datetime; return aware UTC datetime."""
    if isinstance(ts, dt.datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    if isinstance(ts, (int, float)):
        return dt.datetime.fromtimestamp(float(ts), tz=UTC)
    if isinstance(ts, str):
        # try parse iso
        try:
            # 2025-10-21 16:00:00 / 2025-10-21T16:00:00Z / with ms
            s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
            return dt.datetime.fromisoformat(s).astimezone(UTC)
        except Exception:
            pass
    raise ValueError(f"Unsupported ts format: {ts!r}")

def _align_floor(ts: dt.datetime, sec: int) -> dt.datetime:
    s = int(ts.timestamp())
    anchored = s - (s % sec)
    return dt.datetime.fromtimestamp(anchored, tz=UTC)

def _agg_ohlcv(bars: List[dict]) -> dict:
    """Aggregate 1s bars to one bar. bar keys: open, high, low, close, volume, trades, vwap(optional)"""
    if not bars:
        return {}
    open_ = bars[0].get("open", bars[0].get("close"))
    close_ = bars[-1].get("close", open_)
    high_ = max(b.get("high", b.get("close", 0)) for b in bars)
    low_ = min(b.get("low", b.get("close", 0)) for b in bars)
    vol = sum(float(b.get("volume", 0.0)) for b in bars)
    trades = sum(int(b.get("trades", 0)) for b in bars)
    # VWAP: prefer 1s vwap*vol if present, else close*vol
    num = 0.0
    for b in bars:
        px = b.get("vwap")
        if px is None:
            px = b.get("close", open_)
        num += float(px) * float(b.get("volume", 0.0))
    vwap = (num / vol) if vol > 0 else None
    return {
        "open": float(open_),
        "high": float(high_),
        "low": float(low_),
        "close": float(close_),
        "volume": float(vol),
        "trades": int(trades),
        "vwap": None if vwap is None else float(vwap),
    }

def _ema(series: List[float], period: int) -> Optional[float]:
    if len(series) < period or period <= 1:
        return None
    k = 2.0 / (period + 1)
    ema_val = series[0]
    for x in series[1:]:
        ema_val = x * k + ema_val * (1 - k)
    return float(ema_val)

def _rsi(series: List[float], period: int = 14) -> Optional[float]:
    if len(series) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        chg = series[i] - series[i - 1]
        gains.append(max(0.0, chg))
        losses.append(max(0.0, -chg))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

class RealtimeAggregator:
    """
    Keep last N 1s bars per symbol, and derive 5s/1m/5m/15m on demand.
    We choose on-demand bucketing for robustness; N is small enough for speed.
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        maxlen_1s: int = 4000,   # >= 15m (900) * 3 for safety
    ) -> None:
        self._one_sec: Dict[str, deque] = defaultdict(lambda: deque(maxlen=maxlen_1s))
        if symbols:
            for s in symbols:
                _ = self._one_sec[s]  # pre-create deques

    # ---------- public API ----------

    def update_from_1s(self, symbol: str, bar_1s: Dict[str, Any]) -> None:
        """
        Append one 1s bar. bar_1s must contain at least: ts, close
        Optional: open, high, low, volume, trades, vwap
        """
        if not bar_1s:
            return
        ts = _to_dt_utc(bar_1s.get("ts"))
        item = {
            "ts": ts,
            "open": bar_1s.get("open", bar_1s.get("close")),
            "high": bar_1s.get("high", bar_1s.get("close")),
            "low":  bar_1s.get("low",  bar_1s.get("close")),
            "close": float(bar_1s.get("close")),
            "volume": float(bar_1s.get("volume", 0.0)),
            "trades": int(bar_1s.get("trades", 0)),
            "vwap": bar_1s.get("vwap"),
        }
        self._one_sec[symbol].append(item)

    def seed_with_5s(self, symbol: str, bars_5s: List[Dict[str, Any]]) -> None:
        """
        Cold start: expand 5s bars into 5x 1s 'synthetic' bars to populate context quickly.
        This is acceptable for medium/longer TF background; real-time will soon override.
        """
        if not bars_5s:
            return
        for b in sorted(bars_5s, key=lambda x: _to_dt_utc(x["ts"])):
            ts0 = _to_dt_utc(b["ts"])
            o, h, l, c = [b.get(k, b["close"]) for k in ("open", "high", "low", "close")]
            vol = float(b.get("volume", 0.0))
            tr = int(b.get("trades", 0))
            vwap = b.get("vwap")
            # naive split into 5 chunks
            vol_each = vol / 5.0 if vol else 0.0
            tr_each = int(tr / 5) if tr else 0
            # simple linear path between open->close across 5 seconds
            for i in range(5):
                ts_i = ts0 + dt.timedelta(seconds=i)
                # linear interpolate close
                close_i = o + (c - o) * (i / 4.0) if i < 4 else c
                self.update_from_1s(symbol, {
                    "ts": ts_i,
                    "open": close_i, "high": close_i, "low": close_i, "close": close_i,
                    "volume": vol_each, "trades": tr_each, "vwap": vwap
                })

    def get_snapshot(self, symbol: str) -> Dict[str, Any]:
        """
        Return compact multi-timeframe snapshot for prompt:
        {
          "now": "...UTC...",
          "frames": {
            "1s": {"bars":[...], "count": N},
            "5s": {"bars":[...], "count": N},
            "1m": {"bars":[...], "count": N, "ema9":..., "ema21":..., "rsi14":...},
            "5m": {"bars":[...], "count": N},
            "15m":{"bars":[...], "count": N},
          }
        }
        Bars: [{ts_start, ts_end, open, high, low, close, volume, trades, vwap}]
        """
        one_sec = list(self._one_sec[symbol])
        one_sec.sort(key=lambda x: x["ts"])

        snap = {
            "now": dt.datetime.now(tz=UTC).isoformat(),
            "frames": {}
        }
        # 1s: last 60 (raw)
        last_1s = one_sec[-60:]
        snap["frames"]["1s"] = {
            "bars": [
                {"ts": b["ts"].isoformat(), "close": b["close"], "volume": b["volume"], "vwap": b["vwap"]}
                for b in last_1s
            ],
            "count": len(last_1s)
        }
        # Aggregates
        for sec, label, want in [(5, "5s", 12), (60, "1m", 10), (300, "5m", 6), (900, "15m", 4)]:
            agg = self._aggregate(symbol, one_sec, sec, max_windows=want)
            extra = {}
            if label == "1m":
                closes = [bar["close"] for bar in agg]
                ema9 = _ema(closes[-30:], 9) if closes else None
                ema21 = _ema(closes[-60:], 21) if closes else None
                rsi14 = _rsi(closes[-15:], 14) if closes else None
                extra.update({"ema9": ema9, "ema21": ema21, "rsi14": rsi14})
            snap["frames"][label] = {
                "bars": agg,
                "count": len(agg),
                **extra
            }
        return snap

    # ---------- internal ----------

    def _aggregate(
        self, symbol: str, one_sec_sorted: List[dict], window_sec: int, max_windows: int
    ) -> List[dict]:
        if not one_sec_sorted:
            return []
        # group by aligned window
        buckets: Dict[dt.datetime, List[dict]] = defaultdict(list)
        for b in one_sec_sorted:
            w0 = _align_floor(b["ts"], window_sec)
            buckets[w0].append(b)
        keys = sorted(buckets.keys())
        out: List[dict] = []
        for k in keys[-max_windows:]:
            members = buckets[k]
            agg = _agg_ohlcv(members)
            if not agg:
                continue
            out.append({
                "ts_start": k.isoformat(),
                "ts_end": (k + dt.timedelta(seconds=window_sec)).isoformat(),
                **agg
            })
        return out
```

---

# ② 新增文件：`prompt_builder_mtf.py`

> 把多周期快照 + 风险/时间限制拼成 **DeepSeek-R1** 友好的提示语；并返回 `missing_timeframes` 供主程序做**动作降级/封顶**。

```python
# prompt_builder_mtf.py
from __future__ import annotations
import json
from typing import Dict, Any, List

SYSTEM_R1 = (
    "You are an intraday trading analyst. Think step-by-step but return ONLY a JSON object.\n"
    "If data is incomplete for key timeframes, avoid opening new positions."
)

def _missing_frames(mtf: Dict[str, Any], min_need: Dict[str, int]) -> List[str]:
    miss = []
    frames = mtf.get("frames", {})
    for k, need in min_need.items():
        have = int(frames.get(k, {}).get("count", 0))
        if have < need:
            miss.append(k)
    return miss

def build_prompt_for_r1(
    symbol: str,
    mtf_snapshot: Dict[str, Any],
    portfolio: Dict[str, Any],
    vix_value: float | None,
    time_ctx: Dict[str, Any],
    min_need: Dict[str, int],
    cash_limit: float,
    allow_new_position: bool
) -> Dict[str, Any]:
    """
    Return: {"system":..., "user":..., "missing_timeframes":[...]}
    The 'user' message contains compact JSON context for R1.
    """
    missing = _missing_frames(mtf_snapshot, min_need)

    ctx = {
        "symbol": symbol,
        "now_utc": mtf_snapshot.get("now"),
        "time": time_ctx,  # now_et, minutes_to_close, eod_trigger, block_new_positions
        "portfolio": portfolio,  # position_size, avg_cost, cash, equity, unrealized_pnl
        "risk_budget": {"cash_limit": cash_limit},
        "vix": vix_value,
        "data_quality": {
            "missing_timeframes": missing,
            "min_requirements": min_need,
        },
        "multi_timeframe": mtf_snapshot["frames"],
        "instruction": {
            "objective": "Trade intraday with adaptive sizing. Favor confluence across 1m/5m/15m.",
            "risk_rules": [
                "If any of ['1m','5m'] is missing, do NOT open new positions (WAIT/REDUCE/EXIT only).",
                "Closer to EOD, prefer reducing risk or exit.",
                "Use higher size only when volatility is moderate and signals align across timeframes."
            ],
            "actions": ["WAIT","BUY","SELL","REDUCE","EXIT","FLATTEN"]
        },
        "return_format": {
            "action": "one of actions",
            "confidence": "0.0-1.0",
            "risk_units": "0..3 integer",
            "notes": "explain in one sentence",
            "stop_distance_bps": "suggested initial stop distance in bps (optional)"
        }
    }

    # hard gate for new positions
    if (missing and any(x in ("1m","5m") for x in missing)) or (not allow_new_position):
        ctx["instruction"]["hard_gate"] = "NO_NEW_POSITION"

    user = (
        "TASK: Decide next action for the symbol, considering portfolio & time-of-day.\n"
        "Return ONLY JSON as specified by 'return_format'.\n"
        "INPUT_CONTEXT_JSON:\n" + json.dumps(ctx, ensure_ascii=False)
    )
    return {
        "system": SYSTEM_R1,
        "user": user,
        "missing_timeframes": missing
    }
```

---

# ③ 给 `market_data_fetcher.py` 加两段辅助方法（预填充 & VIX）

> 你已有 Redis 1s 读取函数，这里只补：从 ClickHouse 取**最近 N 分钟的 5s bar** 用于冷启动预填充；以及取最新 VIX。

```python
# --- 在 market_data_fetcher.py 顶部 imports 附近补充 ---
from typing import List, Dict, Optional
import datetime as dt

# --- 在 MarketDataFetcher 类中新增方法： ---
    async def prefill_5s_from_clickhouse(self, symbol: str, minutes: int = 20) -> List[Dict]:
        """
        从 ClickHouse sfs.sfs_5s 取最近 N 分钟 5s bar（仅用于冷启动预填充）。
        同步/异步环境按你原文件结构调整；如果你已有同步 client，这里用同步也可以。
        """
        try:
            since = dt.datetime.utcnow() - dt.timedelta(minutes=max(1, int(minutes)) + 2)
            sql = f"""
            SELECT ts, open, high, low, close, vwap, volume, trades
            FROM sfs.sfs_5s
            WHERE symbol = %(sym)s
              AND ts >= %(since)s
            ORDER BY ts ASC
            """
            rows = self.ch_client.execute(sql, params={"sym": symbol, "since": since})
            out = []
            for r in rows:
                ts, o, h, l, c, vwap, vol, tr = r
                out.append({
                    "ts": ts, "open": o, "high": h, "low": l, "close": c,
                    "vwap": vwap, "volume": vol, "trades": tr
                })
            return out
        except Exception as e:
            self.logger.warning(f"[prefill_5s] CH error for {symbol}: {e}")
            return []

    async def get_latest_vix(self) -> Optional[float]:
        """从 refs_vix_ticks 抓一个最近值；没有就返回 None。"""
        try:
            sql = """
            SELECT vix
            FROM trading.refs_vix_ticks
            WHERE ts >= now() - INTERVAL 30 MINUTE
            ORDER BY ts DESC
            LIMIT 1
            """
            rows = self.ch_client.execute(sql)
            return float(rows[0][0]) if rows else None
        except Exception as e:
            self.logger.warning(f"[vix] CH error: {e}")
            return None
```

---

# ④ 修改 `run_single_bot.py`（最小入侵式接入）

> 新增 CLI 参数；创建聚合器；启动时预填充；每个 cycle 用 1s→聚合→构建 R1 提示→下决策；如果**关键周期缺数**或进入“禁止新开仓窗口”，则**主程序层**自动降级为 `WAIT/REDUCE/EXIT`（即便模型想 BUY）。

**A. argparse 处添加参数：**

```python
# --- imports 增加 ---
from realtime_aggregator import RealtimeAggregator
from prompt_builder_mtf import build_prompt_for_r1
from market_time import get_session_bounds, compute_eod_trigger

# --- argparse 新增 ---
parser.add_argument("--use-aggregator", action="store_true", default=True,
                    help="Use realtime in-memory aggregator from 1s bars (default: True).")
parser.add_argument("--prefill-5s-mins", type=int, default=20,
                    help="Cold start: prefill last N minutes of 5s bars from ClickHouse (default: 20).")
parser.add_argument("--min-bars-1m", type=int, default=8, help="Min 1m bars required to allow BUY.")
parser.add_argument("--min-bars-5m", type=int, default=2, help="Min 5m bars required to allow BUY.")
parser.add_argument("--min-bars-15m", type=int, default=1, help="Min 15m bars (soft).")
parser.add_argument("--degrade-if-missing", action="store_true", default=True,
                    help="Degrade actions if key frames missing (default: True).")
parser.add_argument("--fallback-to-planA", action="store_true", default=True,
                    help="If aggregator insufficient, fallback to lightweight mode (1s/5s only).")
```

**B. 启动时创建聚合器并做预填充（在 main 初始化后、进入主循环前）：**

```python
# symbols = args.symbols ...
aggregator = None
if args.use_aggregator:
    aggregator = RealtimeAggregator(symbols=args.symbols)
    if args.prefill_5s_mins > 0:
        for sym in args.symbols:
            try:
                pre5 = await data_fetcher.prefill_5s_from_clickhouse(sym, minutes=args.prefill_5s_mins)
                if pre5:
                    aggregator.seed_with_5s(sym, pre5)
                    logger.info(f"[Aggregator] Prefilled {sym} with {len(pre5)}x5s (~{args.prefill_5s_mins}m)")
            except Exception as e:
                logger.warning(f"[Aggregator] Prefill error {sym}: {e}")
```

**C. 每个 trading cycle 内，用 1s→聚合→Prompt→决策（放在你当前“拉数据+构建prompt”的位置替换/合并）：**

```python
# 时间上下文（配合你已加的 close-at/close-before/no-new-positions-mins）
sb = get_session_bounds()
eod_trigger_et = compute_eod_trigger(None, args.close_at, args.close_before)
time_ctx = {
    "now_et": sb.now_et.strftime("%Y-%m-%d %H:%M:%S"),
    "minutes_to_close": sb.minutes_to_close,
    "eod_trigger_et": eod_trigger_et.strftime("%H:%M"),
    "block_new_positions": sb.minutes_to_close <= max(0, int(args.no_new_positions_mins))
}

for sym in args.symbols:
    # 1) 拉取最新 1s（如果你已有函数 last_n(symbol,1) 就用它；这里用你文件中现成的方法）
    bars_1s = await data_fetcher.get_1s_bars_from_redis(sym, n=1)
    if not bars_1s:
        logger.info(f"[{sym}] no 1s bar, skip this cycle.")
        continue
    last_1s = bars_1s[-1]
    if aggregator:
        aggregator.update_from_1s(sym, last_1s)

    # 2) 生成多周期快照
    mtf = aggregator.get_snapshot(sym) if aggregator else {"frames": {"1s": {"bars": bars_1s, "count": len(bars_1s)}}}

    # 3) 取 VIX（可缓存每 N 周期再取）
    vix_val = await data_fetcher.get_latest_vix()

    # 4) 组合最小样本门槛 & 是否允许开新仓
    min_need = {"1m": args.min_bars_1m, "5m": args.min_bars_5m, "15m": args.min_bars_15m}
    allow_new = (not time_ctx["block_new_positions"])
    # 5) 组合 portfolio 快照（你现有的）
    portfolio = portfolio_mgr.snapshot(sym)  # e.g. {"position_size":.., "avg_cost":.., "cash":.., "equity":..}

    # 6) 构建 R1 提示
    prompt_pack = build_prompt_for_r1(
        symbol=sym,
        mtf_snapshot=mtf,
        portfolio=portfolio,
        vix_value=vix_val,
        time_ctx=time_ctx,
        min_need=min_need,
        cash_limit=args.cash,
        allow_new_position=allow_new
    )

    # 7) 调用 Provider（保持你现有接口；若需要仅传 prompt 字符串就用 prompt_pack["user"]）
    #    例如：decision = provider.decide(prompt_pack["system"], prompt_pack["user"])
    #    或：decision = provider.decide_with_messages([{"role":"system","content":...},{"role":"user","content":...}])
    decision = provider.decide(system_prompt=prompt_pack["system"], user_prompt=prompt_pack["user"])

    # 8) 主程序层的“缺数降级/禁止开新仓”保护（即便 LLM 想 BUY，也先挡住）
    missing = prompt_pack["missing_timeframes"]
    if args.degrade_if_missing and (("1m" in missing) or ("5m" in missing)) and portfolio.get("position_size", 0) <= 0:
        if decision and isinstance(decision, dict):
            decision["action"] = "WAIT"
            decision["risk_units"] = 0
            decision["notes"] = (decision.get("notes") or "") + " | downgraded: missing 1m/5m"
        logger.info(f"[{sym}] downgraded to WAIT due to missing frames: {missing}")

    if time_ctx["block_new_positions"] and portfolio.get("position_size", 0) <= 0 and decision.get("action") == "BUY":
        decision["action"] = "WAIT"
        decision["risk_units"] = 0
        decision["notes"] = (decision.get("notes") or "") + " | block new positions window"
        logger.info(f"[{sym}] BUY blocked in no-new-positions window.")

    # 9) 后续走你现有的“risk_units -> shares -> 下单或模拟成交”的通道即可
    #    （若 decision 中返回 stop_distance_bps，可作为建议止损距离）
```

> ⚠️ 如果你的 `provider` 不是 `decide(system_prompt, user_prompt)` 这种签名，就把 `prompt_pack["user"]` 直接拼成你现有的 prompt 输入形式；`prompt_pack["system"]` 放 system 条即可。R1 的 JSON-only 要求在 `system` 里已声明。

---

## 行为校验（你可以用日志快速验证）

* **冷启动 1–5 分钟内**：日志应显示 `missing_timeframes` 包含 `1m` 或 `5m`，动作被**降级为 WAIT**；有持仓时允许 `REDUCE/EXIT`。
* **聚合完整后**：LLM 才可能给出 `BUY`；并根据 1m/5m/15m 一致性与 VIX 调整 `risk_units`。
* **收盘前 N 分钟**：即使信号很好，也会被主程序**拦截新开仓**（但允许减仓/平仓）。
* **到达清仓触发 `close-at / close-before`**：平掉持仓并优雅退出（之前我们已加过）。

---

## 小结

* 你现在具备：**实时多周期视角（1s→15m）**、**缺数显式降级**、**R1 友好的 Prompt**、**时间窗禁止新开仓**，且**不依赖 ClickHouse features 是否盘中更新**。
* 后续再把“记忆摘要 + 日终回顾 + 自适应频率 + Provider 退避”逐个补上，就非常接近“像人一样有全局意识”的全天交易员了。

需要我顺手把 **DeepSeek-R1 / -fast / OpenAI / Gemini** 这几个 provider 的 `decide(...)` 统一一个签名吗？如果你点头，我就给一份短小的 `providers/base.py` 接口和各 provider 的适配层，减少你在 run_single_bot 上的分支判断。
好问题！两块一起补齐：

1. **滑动（rolling）features**：让 LLM 不只是“看K线”，而是直接吃到关键技术状态（动量、均值回复、波动、量能、VWAP 偏离、微结构等），省去它在 prompt 里“推导公式”的负担。
2. **更大周期聚合**：给它“鸟瞰图”（30m/1h 甚至日级）来判断当下是在趋势段、震荡段、还是极端波动时段，从而动态调整节奏和仓位。

下面给你：设计要点 + 可直接落地的增量代码（在你之前的 `realtime_aggregator.py / prompt_builder_mtf.py` 基础上升级）。

---

# 设计要点（简洁版）

* **实时主视角**：基于 1s → 聚合出 5s / 1m / 5m / 15m（强化）。
* **更大周期**：在内存可承受的前提下，再加 30m / 1h 实时聚合；>1h（4h/1d）建议用 ClickHouse 每几分钟取一次**参考快照**（不会作为“开新仓”的唯一依据，但能指导风格）。
* **滑动特征**（按时间框架自动计算，缺数据返回 None）：

  * 趋势/动量：`ema_9/21`、`ema_cross`、`slope_20`（简单回归斜率）、`rsi_14`
  * 均值回复：`zscore_close_20/60`、`bollinger_pct_20`、`boll_bw_20`
  * 波动/风险：`rv_20`（实现波动）、`atr_14`（TR 近似）、`range_pct`
  * 量能/微结构：`rvol_roll_20`、`trade_count_ratio`、`avg_trade_size`、`uptick_ratio_60s`、`tick_imbalance_60s`
  * VWAP 偏离：`vwap_win`（窗口内 VWAP）、`vwap_dev_bps`
* **Prompt 供数**：每个 timeframe 附带 `features` 小节（仅十来个核心特征），LLM 可以直接决策，不用在自然语言里倒腾公式。
* **资源与性能**：

  * 1h 需要 ~3600 条 1s 缓冲；建议把 `maxlen_1s` 提到 10k~20k（~几十MB上限，多 symbol 也 OK）。
  * 4h/1d 用 ClickHouse `bars_1h_current / features_1d` 做“背景快照”，每 5-15 分钟刷新一次写进 prompt（不强制实时）。

---

# 代码：升级版实时聚合 + 滑动特征

## ① 覆盖更新 `realtime_aggregator.py`

> 在你现有文件基础上**替换**，增加了 30m/1h 聚合和一组滚动特征计算；默认 `maxlen_1s=20000` 可支持到 1h+。

```python
# realtime_aggregator.py (upgraded)
from __future__ import annotations
import datetime as dt
from collections import deque, defaultdict
from typing import Dict, List, Optional, Any
import math

UTC = dt.timezone.utc

def _to_dt_utc(ts: Any) -> dt.datetime:
    if isinstance(ts, dt.datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    if isinstance(ts, (int, float)):
        return dt.datetime.fromtimestamp(float(ts), tz=UTC)
    if isinstance(ts, str):
        try:
            s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
            return dt.datetime.fromisoformat(s).astimezone(UTC)
        except Exception:
            pass
    raise ValueError(f"Unsupported ts format: {ts!r}")

def _align_floor(ts: dt.datetime, sec: int) -> dt.datetime:
    s = int(ts.timestamp())
    anchored = s - (s % sec)
    return dt.datetime.fromtimestamp(anchored, tz=UTC)

def _agg_ohlcv(bars: List[dict]) -> dict:
    if not bars:
        return {}
    open_ = bars[0].get("open", bars[0].get("close"))
    close_ = bars[-1].get("close", open_)
    high_ = max(b.get("high", b.get("close", 0)) for b in bars)
    low_  = min(b.get("low",  b.get("close", 0)) for b in bars)
    vol   = sum(float(b.get("volume", 0.0)) for b in bars)
    trades= sum(int(b.get("trades", 0)) for b in bars)
    num = 0.0
    for b in bars:
        px = b.get("vwap")
        if px is None:
            px = b.get("close", open_)
        num += float(px) * float(b.get("volume", 0.0))
    vwap = (num / vol) if vol > 0 else None
    return {
        "open": float(open_),
        "high": float(high_),
        "low":  float(low_),
        "close": float(close_),
        "volume": float(vol),
        "trades": int(trades),
        "vwap": None if vwap is None else float(vwap),
    }

def _ema(vals: List[float], period: int) -> Optional[float]:
    if period <= 1 or len(vals) < period:
        return None
    k = 2.0 / (period + 1)
    e = vals[0]
    for x in vals[1:]:
        e = x * k + e * (1 - k)
    return float(e)

def _rsi(vals: List[float], period: int = 14) -> Optional[float]:
    if len(vals) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        chg = vals[-i] - vals[-i - 1]
        gains += max(0.0, chg)
        losses += max(0.0, -chg)
    gains /= period
    losses /= period
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

def _zscore(vals: List[float], lookback: int = 20) -> Optional[float]:
    if len(vals) < lookback:
        return None
    window = vals[-lookback:]
    mu = sum(window) / lookback
    var = sum((x - mu) ** 2 for x in window) / lookback
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return float((vals[-1] - mu) / sd)

def _bollinger(vals: List[float], lookback: int = 20) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    if len(vals) < lookback:
        return None, None, None, None
    window = vals[-lookback:]
    mu = sum(window) / lookback
    var = sum((x - mu) ** 2 for x in window) / lookback
    sd = math.sqrt(var)
    mid = mu
    up = mu + 2 * sd
    dn = mu - 2 * sd
    width = (up - dn) / mid if mid else None
    if up is None or dn is None:
        pct = None
    else:
        rng = up - dn
        pct = (vals[-1] - dn) / rng if rng > 0 else None
    return float(mid), float(up), float(dn), (None if width is None else float(width)), (None if pct is None else float(pct))

def _realized_vol(logrets: List[float], lookback: int = 20) -> Optional[float]:
    if len(logrets) < lookback:
        return None
    window = logrets[-lookback:]
    return float(math.sqrt(sum(x * x for x in window)))

def _atr_like(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1 or len(highs) < period or len(lows) < period:
        return None
    trs = []
    for i in range(1, period + 1):
        h, l, pc = highs[-i], lows[-i], closes[-i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return float(sum(trs) / period)

def _linreg_slope(vals: List[float], lookback: int = 20) -> Optional[float]:
    """Simple slope of y ~ x on last lookback; x = 0..lookback-1"""
    if len(vals) < lookback:
        return None
    y = vals[-lookback:]
    n = lookback
    sx = (n - 1) * n / 2.0
    sx2 = (n - 1) * n * (2 * n - 1) / 6.0
    sy = sum(y)
    sxy = sum(i * y[i] for i in range(n))
    denom = (n * sx2 - sx * sx)
    if denom == 0:
        return 0.0
    slope = (n * sxy - sx * sy) / denom
    return float(slope)

def _safe_mean(a: List[float]) -> Optional[float]:
    return float(sum(a) / len(a)) if a else None

class RealtimeAggregator:
    """
    Keep last N 1s bars per symbol; derive 5s/1m/5m/15m/30m/1h and compute rolling features.
    """
    # 需要更大视角就把 maxlen_1s 调大（比如 20000 支持 >5h）
    def __init__(self, symbols: Optional[List[str]] = None, maxlen_1s: int = 20000) -> None:
        self._one_sec: Dict[str, deque] = defaultdict(lambda: deque(maxlen=maxlen_1s))
        if symbols:
            for s in symbols:
                _ = self._one_sec[s]

        # 你可以在这里调整要导出的时间框架（sec, label, last_n_bars）
        self._frames_plan = [
            (1,   "1s", 60),
            (5,   "5s", 12),
            (60,  "1m", 30),   # 增至30根，足够算 ema/rsi/bb/zscore
            (300, "5m", 12),
            (900, "15m", 8),
            (1800,"30m", 6),   # 新增
            (3600,"1h",  6),   # 新增
        ]

    def update_from_1s(self, symbol: str, bar_1s: Dict[str, Any]) -> None:
        if not bar_1s:
            return
        ts = _to_dt_utc(bar_1s.get("ts"))
        item = {
            "ts": ts,
            "open": bar_1s.get("open", bar_1s.get("close")),
            "high": bar_1s.get("high", bar_1s.get("close")),
            "low":  bar_1s.get("low",  bar_1s.get("close")),
            "close": float(bar_1s.get("close")),
            "volume": float(bar_1s.get("volume", 0.0)),
            "trades": int(bar_1s.get("trades", 0)),
            "vwap": bar_1s.get("vwap"),
        }
        self._one_sec[symbol].append(item)

    def seed_with_5s(self, symbol: str, bars_5s: List[Dict[str, Any]]) -> None:
        if not bars_5s:
            return
        for b in sorted(bars_5s, key=lambda x: _to_dt_utc(x["ts"])):
            ts0 = _to_dt_utc(b["ts"])
            o, h, l, c = [b.get(k, b["close"]) for k in ("open", "high", "low", "close")]
            vol = float(b.get("volume", 0.0))
            tr  = int(b.get("trades", 0))
            vwap= b.get("vwap")
            vol_each = vol / 5.0 if vol else 0.0
            tr_each  = int(tr / 5) if tr else 0
            for i in range(5):
                ts_i = ts0 + dt.timedelta(seconds=i)
                close_i = o + (c - o) * (i / 4.0) if i < 4 else c
                self.update_from_1s(symbol, {
                    "ts": ts_i,
                    "open": close_i, "high": close_i, "low": close_i, "close": close_i,
                    "volume": vol_each, "trades": tr_each, "vwap": vwap
                })

    def _aggregate(self, one_sec_sorted: List[dict], window_sec: int, max_windows: int) -> List[dict]:
        if not one_sec_sorted:
            return []
        buckets: Dict[dt.datetime, List[dict]] = defaultdict(list)
        for b in one_sec_sorted:
            w0 = _align_floor(b["ts"], window_sec)
            buckets[w0].append(b)
        keys = sorted(buckets.keys())
        out: List[dict] = []
        for k in keys[-max_windows:]:
            members = buckets[k]
            agg = _agg_ohlcv(members)
            if not agg:
                continue
            out.append({
                "ts_start": k.isoformat(),
                "ts_end": (k + dt.timedelta(seconds=window_sec)).isoformat(),
                **agg
            })
        return out

    def _compute_features_for_frame(self, bars: List[dict]) -> Dict[str, Optional[float]]:
        if not bars:
            return {}

        closes = [b["close"] for b in bars]
        highs  = [b.get("high", b["close"]) for b in bars]
        lows   = [b.get("low",  b["close"]) for b in bars]
        vols   = [float(b.get("volume", 0.0)) for b in bars]
        trades = [float(b.get("trades", 0.0)) for b in bars]
        vwaps  = [b.get("vwap") for b in bars]
        # log returns
        logrets = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0 and closes[i] > 0:
                logrets.append(math.log(closes[i] / closes[i - 1]))
            else:
                logrets.append(0.0)

        ema9  = _ema(closes, 9)
        ema21 = _ema(closes, 21)
        ema_cross = (ema9 - ema21) if (ema9 is not None and ema21 is not None) else None
        rsi14 = _rsi(closes, 14)
        z20   = _zscore(closes, 20)
        z60   = _zscore(closes, 60)
        bb = _bollinger(closes, 20)
        if bb[0] is None:
            bb_mid = bb_up = bb_dn = bb_bw = bb_pct = None
        else:
            bb_mid, bb_up, bb_dn, bb_bw, bb_pct = bb  # type: ignore
        rv20 = _realized_vol(logrets, 20)
        atr14= _atr_like(highs, lows, closes, 14)
        rng  = (max(highs[-20:]) - min(lows[-20:])) / closes[-1] if len(highs) >= 20 else None

        # rvol: 当前均量/滚动均量 (20)
        vol_mean20 = _safe_mean(vols[-20:])
        rvol_roll_20 = (vols[-1] / vol_mean20) if (vol_mean20 and vol_mean20 > 0) else None
        trade_mean20 = _safe_mean(trades[-20:])
        trade_count_ratio = (trades[-1] / trade_mean20) if (trade_mean20 and trade_mean20 > 0) else None
        avg_trade_size = (vols[-1] / trades[-1]) if (trades[-1] and trades[-1] > 0) else None

        # 窗口 VWAP 与偏离（基于 bar vwap*volume / sum(volume)）
        vnum, vden = 0.0, 0.0
        for i, b in enumerate(bars[-20:]):
            px = b.get("vwap")
            if px is None: px = b["close"]
            v = float(b.get("volume", 0.0))
            vnum += px * v
            vden += v
        vwap_win = (vnum / vden) if vden > 0 else None
        if vwap_win is None:
            vwap_dev_bps = None
        else:
            vwap_dev_bps = (closes[-1] / vwap_win - 1.0) * 10000.0

        # 微结构（1s帧专属，会从外层填充；其他帧留空）
        features = {
            "ema_9": ema9, "ema_21": ema21, "ema_cross": ema_cross,
            "rsi_14": rsi14,
            "zscore_close_20": z20, "zscore_close_60": z60,
            "bb_mid_20": bb_mid, "bb_up_20": bb_up, "bb_dn_20": bb_dn,
            "bb_bw_20": bb_bw, "bb_pct_20": bb_pct,
            "rv_20": rv20, "atr_14": atr14, "range_pct_20": rng,
            "rvol_roll_20": rvol_roll_20,
            "trade_count_ratio": trade_count_ratio,
            "avg_trade_size": avg_trade_size,
            "vwap_window": vwap_win, "vwap_dev_bps": vwap_dev_bps,
        }
        return {k: (None if (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else v)
                for k, v in features.items()}

    def _micro_features_1s(self, last_1s: List[dict]) -> Dict[str, Optional[float]]:
        if not last_1s:
            return {}
        closes = [b["close"] for b in last_1s]
        ups = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
        downs = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i - 1])
        total = ups + downs
        uptick_ratio = ups / total if total > 0 else None
        tick_imb = (ups - downs) / total if total > 0 else None
        mom_10s = (closes[-1] / closes[-10] - 1.0) if len(closes) >= 10 and closes[-10] > 0 else None
        return {
            "uptick_ratio_60s": uptick_ratio,
            "tick_imbalance_60s": tick_imb,
            "momentum_10s": mom_10s
        }

    def get_snapshot(self, symbol: str) -> Dict[str, Any]:
        one_sec = list(self._one_sec[symbol])
        one_sec.sort(key=lambda x: x["ts"])

        snap = {
            "now": dt.datetime.now(tz=UTC).isoformat(),
            "frames": {}
        }

        # 逐个 timeframe 聚合 + 特征计算
        for sec, label, want in self._frames_plan:
            if label == "1s":
                last_1s = one_sec[-want:]
                # 1s bars 用精简字段，附加微结构特征
                snap["frames"]["1s"] = {
                    "bars": [
                        {"ts": b["ts"].isoformat(), "close": b["close"],
                         "volume": b["volume"], "vwap": b["vwap"], "trades": b["trades"]}
                        for b in last_1s
                    ],
                    "count": len(last_1s),
                    "features": self._micro_features_1s(last_1s)
                }
            else:
                agg = self._aggregate(one_sec, sec, want)
                feats = self._compute_features_for_frame(agg)
                snap["frames"][label] = {
                    "bars": agg,
                    "count": len(agg),
                    "features": feats
                }
        return snap
```

---

## ② 更新 `prompt_builder_mtf.py`

> Prompt 里把 **features** 也放进去（每个 timeframe 的 `features` 字段已经包含在 `frames[label].features`），并加一条指令：**更大周期优先确定“风格”（趋势/震荡/极端波动），短周期决定“执行”（进出/加减仓）**。

```python
# prompt_builder_mtf.py (delta)
from __future__ import annotations
import json
from typing import Dict, Any, List

SYSTEM_R1 = (
    "You are an intraday trading analyst. Think step-by-step but return ONLY a JSON object.\n"
    "Use multi-timeframe features: longer frames define regime, shorter frames time entries/exits.\n"
    "If key frames missing or end-of-day window, avoid opening new positions."
)

def _missing_frames(mtf: Dict[str, Any], min_need: Dict[str, int]) -> List[str]:
    miss = []
    frames = mtf.get("frames", {})
    for k, need in min_need.items():
        have = int(frames.get(k, {}).get("count", 0))
        if have < need:
            miss.append(k)
    return miss

def build_prompt_for_r1(
    symbol: str,
    mtf_snapshot: Dict[str, Any],
    portfolio: Dict[str, Any],
    vix_value: float | None,
    time_ctx: Dict[str, Any],
    min_need: Dict[str, int],
    cash_limit: float,
    allow_new_position: bool,
    higher_tf_context: Dict[str, Any] | None = None  # 可把 4h/1d 背景快照塞进来
) -> Dict[str, Any]:
    missing = _missing_frames(mtf_snapshot, min_need)

    ctx = {
        "symbol": symbol,
        "now_utc": mtf_snapshot.get("now"),
        "time": time_ctx,
        "portfolio": portfolio,
        "risk_budget": {"cash_limit": cash_limit},
        "vix": vix_value,
        "data_quality": {
            "missing_timeframes": missing,
            "min_requirements": min_need,
        },
        "multi_timeframe": mtf_snapshot["frames"],   # 已含 features
        "higher_timeframe_context": higher_tf_context or {},  # 例如 { "1h": {...}, "1d": {...} }
        "instruction": {
            "objective": "Trade intraday with adaptive sizing based on regime and confluence.",
            "regime_determination": "Use 30m/1h features (ema_cross, rsi, zscores, rv, atr) to determine trend/mean-reverting/high-vol regimes.",
            "execution_logic": "Use 1m/5m and micro 1s/5s to time entries/exits and scale risk_units.",
            "risk_rules": [
                "If any of ['1m','5m'] missing, do NOT open new positions.",
                "Near EOD, reduce or exit rather than opening new positions.",
                "Higher risk_units only when volatility moderate and confluence across frames."
            ],
            "actions": ["WAIT","BUY","SELL","REDUCE","EXIT","FLATTEN"]
        },
        "return_format": {
            "action": "one of actions",
            "confidence": "0.0-1.0",
            "risk_units": "0..3 integer",
            "notes": "one sentence rationale referencing features",
            "stop_distance_bps": "optional float"
        }
    }

    if (missing and any(x in ("1m","5m") for x in missing)) or (not allow_new_position):
        ctx["instruction"]["hard_gate"] = "NO_NEW_POSITION"

    user = (
        "TASK: Decide next action based on multi-timeframe features and portfolio state. Respond ONLY JSON.\n"
        "INPUT_CONTEXT_JSON:\n" + json.dumps(ctx, ensure_ascii=False)
    )
    return {"system": SYSTEM_R1, "user": user, "missing_timeframes": missing}
```

---

## ③ 更大周期的“背景快照”（可选增强）

* 对于 **4h / 1d**：建议每 **5~15 分钟**从 ClickHouse 拿一次（`bars_1h_current` 做 4h 滚动窗口聚合、`features_1d` 直接取昨日日线特征），放进 `higher_timeframe_context`。
* 这是“风格锚点”，不强制实时；LLM 用它判断是**趋势日**还是**震荡日**，从而调整是否“快进快出”。

你已有 CH 连接的话，加一个简短的取数方法即可（伪代码）：

```python
# 每N分钟调用一次，缓存到变量供 prompt_builder 使用
def load_higher_tf_context(ch, symbol) -> dict:
    # 1) 近几小时的 1h/4h 简要特征（用 bars_1h_current 或你自己的视图）
    # 2) 1d 的 ATR、zscore、前高前低等
    # 返回 {"1h": {...features...}, "1d": {...features...}}
    ...
```

在 `build_prompt_for_r1(..., higher_tf_context=ctx_1h_1d)` 传入即可。

---

# 你会得到的实际收益

* **更稳的开仓门槛**：没有 1m/5m 就不许开，减少“假启动”；有了 30m/1h 的 regime，LLM 不会在大级别下跌时拼命接飞刀。
* **更好的仓位分配**：`risk_units` 将跟随 `rv_20/atr_14/bb_bw_20` 等波动因子自动变化；宽带震荡→降杠杆。
* **解释性强**：LLM 会在 `notes` 里引用 features（比如“1h ema_cross<0 且 5m rsi_14 < 35，vwap_dev_bps -22 → REDUCE/EXIT”），便于你回溯。

---

如果你点头，我可以再给你两段小补丁：

* `market_data_fetcher.py`：一个 `load_higher_tf_context()` 的 ClickHouse 查询实现（1h/1d）；
* `run_single_bot.py`：把 `higher_tf_context` 注入 prompt，且每 5~10 个 cycle 只刷新一次，避免打CH过频。

要不要我现在就把这两段也贴上来？
太棒了。按你刚才的确认，这里给出**可直接落地的代码**，覆盖三个新增模块 + 两处集成补丁——分别实现：

* 实时多周期聚合（`RealtimeAggregator`）
* 更大周期背景快照（1h/1d，`HigherTFContextLoader`）
* 交易时段时间管理（`TimeManager`：`--close-at / --close-before`、收盘前禁止开新仓、EOD 强制清仓）
* DeepSeek-R1 专用 Prompt 构建器（带多周期 + 高阶周期 + 时间/仓位上下文）

> 你只需把下面 5 段代码粘到对应**新文件**中（或合并到你的现有文件中），然后按最后一段的 **run_single_bot.py 集成补丁** 把参数与调用接上即可。

---

# 1) 新建：`llm_trader/realtime_aggregator.py`

```python
# llm_trader/realtime_aggregator.py
from collections import deque
from typing import Dict, Deque, Any, List, Optional
import math
import datetime as dt

class RealtimeAggregator:
    """
    从 1s bars 增量聚合出 5s/1m/5m/15m/30m/1h，并计算一批轻量滚动特征。
    只依赖传入的 1s bar（字典：ts, open, high, low, close, volume）。
    """

    def __init__(self, lookbacks: Dict[str, int] = None):
        # 保存每个周期的 deque；长度设为覆盖交易日 + 少量冗余
        self.lookbacks = lookbacks or {
            "1s":  3600,   # 1 小时
            "5s":  720,    # 1 小时
            "1m":  600,    # ~10 小时
            "5m":  200,    # ~16 小时
            "15m": 100,    # ~25 小时
            "30m": 100,
            "1h":  200,
        }
        self.bars: Dict[str, Dict[str, Deque[dict]]] = {}
        self._bucket = {"5s": 5, "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}

    # ----------------- 工具函数（技术指标） -----------------
    @staticmethod
    def _ema(vals: List[float], period: int) -> Optional[float]:
        if period <= 1 or len(vals) < period:
            return None
        k = 2.0 / (period + 1)
        e = vals[-period]
        for x in vals[-period+1:]:
            e = x * k + e * (1 - k)
        return float(e)

    @staticmethod
    def _rsi(vals: List[float], period: int = 14) -> Optional[float]:
        if len(vals) < period + 1:
            return None
        gains = 0.0
        losses = 0.0
        for i in range(1, period + 1):
            chg = vals[-i] - vals[-i - 1]
            gains += max(0.0, chg)
            losses += max(0.0, -chg)
        gains /= period
        losses /= period
        if losses == 0:
            return 100.0
        rs = gains / losses
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _zscore(vals: List[float], lookback: int) -> Optional[float]:
        if len(vals) < lookback:
            return None
        window = vals[-lookback:]
        mu = sum(window) / lookback
        var = sum((x - mu) ** 2 for x in window) / lookback
        sd = math.sqrt(var)
        if sd == 0:
            return 0.0
        return float((vals[-1] - mu) / sd)

    @staticmethod
    def _vwap(bars: List[dict], n: int) -> Optional[float]:
        if len(bars) < n:
            return None
        wsum = 0.0
        vsum = 0.0
        for b in bars[-n:]:
            price = (b["high"] + b["low"] + b["close"]) / 3.0
            vol = b.get("volume", 0.0) or 0.0
            wsum += price * vol
            vsum += vol
        return float(wsum / vsum) if vsum > 0 else None

    # ----------------- 数据结构初始化 -----------------
    def _ensure_symbol(self, symbol: str):
        if symbol not in self.bars:
            self.bars[symbol] = {tf: deque(maxlen=self.lookbacks[tf]) for tf in self.lookbacks}

    # ----------------- 写入 1s，向上聚合 -----------------
    def update_from_1s(self, symbol: str, bar_1s: dict):
        """
        bar_1s 必须包含 keys: ts (datetime/iso), open, high, low, close, volume
        """
        self._ensure_symbol(symbol)
        # 1) 追加 1s
        self._append_bar(symbol, "1s", bar_1s)

        # 2) 触发聚合到更大周期
        self._cascade_aggregate(symbol, base="1s", up="5s")
        self._cascade_aggregate(symbol, base="5s", up="1m")
        self._cascade_aggregate(symbol, base="1m", up="5m")
        self._cascade_aggregate(symbol, base="5m", up="15m")
        self._cascade_aggregate(symbol, base="15m", up="30m")
        self._cascade_aggregate(symbol, base="30m", up="1h")

    def _append_bar(self, symbol: str, tf: str, bar: dict):
        self.bars[symbol][tf].append(bar)

    def _cascade_aggregate(self, symbol: str, base: str, up: str):
        """
        每有 _bucket[up]/_bucket[base] 根 base bar，聚合出 1 根 up bar。
        简化：按固定窗口聚合（不跨天），由调用方保证输入是连续的交易时间序列。
        """
        if up not in self._bucket or base not in self._bucket:
            return
        base_bucket = self._bucket[base]
        up_bucket = self._bucket[up]
        ratio = up_bucket // base_bucket
        base_deque = self.bars[symbol][base]
        if len(base_deque) < ratio:
            return
        # 如果 up 的最后一根已经对齐最新窗口，就不再重复生成
        recent = list(base_deque)[-ratio:]
        o = recent[0]["open"]
        h = max(b["high"] for b in recent)
        l = min(b["low"] for b in recent)
        c = recent[-1]["close"]
        v = sum((b.get("volume") or 0.0) for b in recent)
        up_bar = {
            "ts": recent[-1]["ts"],
            "open": o, "high": h, "low": l, "close": c, "volume": v
        }
        # 避免重复：如果 up 已存在且 ts 相同则覆盖最近一根
        up_deque = self.bars[symbol][up]
        if len(up_deque) and up_deque[-1]["ts"] == up_bar["ts"]:
            up_deque[-1] = up_bar
        else:
            up_deque.append(up_bar)

    # ----------------- 取快照 + 计算特征 -----------------
    def _build_features(self, bars: Deque[dict]) -> Dict[str, Optional[float]]:
        closes = [b["close"] for b in bars]
        feats = {
            "ema_9":   self._ema(closes, 9)   if len(closes) >= 9  else None,
            "ema_21":  self._ema(closes, 21)  if len(closes) >= 21 else None,
            "rsi_14":  self._rsi(closes, 14)  if len(closes) >= 15 else None,
            "z_20":    self._zscore(closes, 20) if len(closes) >= 20 else None,
            "z_60":    self._zscore(closes, 60) if len(closes) >= 60 else None,
            "vwap_20": self._vwap(list(bars), 20) if len(bars) >= 20 else None,
        }
        if feats["ema_9"] is not None and feats["ema_21"] is not None:
            feats["ema_cross"] = feats["ema_9"] - feats["ema_21"]
        else:
            feats["ema_cross"] = None
        return feats

    def get_snapshot(self, symbol: str) -> Dict[str, Any]:
        """返回多周期 bars + 每个周期滚动特征。"""
        self._ensure_symbol(symbol)
        out: Dict[str, Any] = {}
        for tf in ["1s","5s","1m","5m","15m","30m","1h"]:
            bq = self.bars[symbol][tf]
            out[tf] = {
                "bars": list(bq)[-120:],   # 控制 prompt 体积
                "features": self._build_features(bq) if len(bq) else {}
            }
        return out
```

---

# 2) 新建：`llm_trader/higher_tf_context.py`

```python
# llm_trader/higher_tf_context.py
from typing import Dict, Any, List, Optional
import math

class HigherTFContextLoader:
    """
    从 ClickHouse 拉 1h K线（近 N 根）并计算滚动特征，
    同时抓取 1d features 最新一条，作为背景锚点（即便是昨日日终也有参考价值）。
    """
    def __init__(self, ch_client):
        self.ch = ch_client

    # ---------- 指标 ----------
    @staticmethod
    def _ema(vals: List[float], period: int) -> Optional[float]:
        if period <= 1 or len(vals) < period:
            return None
        k = 2.0 / (period + 1)
        e = vals[-period]
        for x in vals[-period+1:]:
            e = x * k + e * (1 - k)
        return float(e)

    @staticmethod
    def _rsi(vals: List[float], period: int = 14) -> Optional[float]:
        if len(vals) < period + 1:
            return None
        gains = 0.0
        losses = 0.0
        for i in range(1, period + 1):
            chg = vals[-i] - vals[-i - 1]
            gains += max(0.0, chg)
            losses += max(0.0, -chg)
        gains /= period
        losses /= period
        if losses == 0:
            return 100.0
        rs = gains / losses
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _z(vals: List[float], lookback: int) -> Optional[float]:
        if len(vals) < lookback:
            return None
        window = vals[-lookback:]
        mu = sum(window) / lookback
        var = sum((x - mu) ** 2 for x in window) / lookback
        sd = math.sqrt(var)
        return 0.0 if sd == 0 else float((vals[-1] - mu) / sd)

    @staticmethod
    def _boll(vals: List[float], n: int = 20):
        if len(vals) < n:
            return None, None, None, None
        w = vals[-n:]
        mu = sum(w) / n
        var = sum((x - mu) ** 2 for x in w) / n
        sd = math.sqrt(var)
        mid = mu
        up = mu + 2 * sd
        dn = mu - 2 * sd
        width = (up - dn) / mid if mid else None
        return float(mid), float(up), float(dn), (None if width is None else float(width))

    @staticmethod
    def _atr_like(highs: List[float], lows: List[float], closes: List[float], n: int = 14) -> Optional[float]:
        if len(closes) < n + 1:
            return None
        trs = []
        for i in range(1, n + 1):
            h, l, pc = highs[-i], lows[-i], closes[-i - 1]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        return float(sum(trs) / n)

    # ---------- ClickHouse ----------
    def _fetch_1h(self, symbol: str, limit: int) -> List[dict]:
        q = f"""
        SELECT ts_start, open, high, low, close, volume
        FROM trading.bars_1h_current
        WHERE symbol = %(symbol)s
        ORDER BY ts_start DESC
        LIMIT {int(limit)}
        """
        rows = self.ch.execute(q, params={"symbol": symbol}) or []
        bars = []
        for r in rows[::-1]:
            bars.append({
                "ts": r[0], "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])
            })
        return bars

    def _fetch_1d_feature(self, symbol: str) -> Optional[dict]:
        q = """
        SELECT
          ts, close, vwap, atr_14, ema_20, ema_50, rsi_14,
          zscore_close_20, zscore_close_60, vwap_day, vwap_dev_bps
        FROM trading.features_1d
        WHERE symbol = %(symbol)s
        ORDER BY ts DESC
        LIMIT 1
        """
        r = self.ch.execute(q, params={"symbol": symbol}) or []
        if not r:
            return None
        x = r[0]
        return {
            "ts": x[0],
            "close": float(x[1]),
            "vwap": float(x[2]) if x[2] is not None else None,
            "atr_14": float(x[3]) if x[3] is not None else None,
            "ema_20": float(x[4]) if x[4] is not None else None,
            "ema_50": float(x[5]) if x[5] is not None else None,
            "rsi_14": float(x[6]) if x[6] is not None else None,
            "zscore_close_20": float(x[7]) if x[7] is not None else None,
            "zscore_close_60": float(x[8]) if x[8] is not None else None,
            "vwap_day": float(x[9]) if x[9] is not None else None,
            "vwap_dev_bps": float(x[10]) if x[10] is not None else None,
        }

    # ---------- 对外 ----------
    def load_symbol(self, symbol: str, one_hour_limit: int = 120) -> Dict[str, Any]:
        out: Dict[str, Any] = {"1h": {"bars": [], "features": {}}, "1d": {"features": {}}}
        bars = self._fetch_1h(symbol, one_hour_limit)
        out["1h"]["bars"] = bars[-24:]  # 控制体积

        if bars:
            closes = [b["close"] for b in bars]
            highs = [b["high"] for b in bars]
            lows  = [b["low"]  for b in bars]
            ema9 = self._ema(closes, 9)
            ema21 = self._ema(closes, 21)
            rsi14 = self._rsi(closes, 14)
            z20 = self._z(closes, 20)
            z60 = self._z(closes, 60)
            bb_mid, bb_up, bb_dn, bb_w = self._boll(closes, 20)
            atr14 = self._atr_like(highs, lows, closes, 14)
            out["1h"]["features"] = {
                "ema_9": ema9, "ema_21": ema21,
                "ema_cross": (ema9 - ema21) if (ema9 is not None and ema21 is not None) else None,
                "rsi_14": rsi14,
                "zscore_close_20": z20, "zscore_close_60": z60,
                "bb_mid_20": bb_mid, "bb_up_20": bb_up, "bb_dn_20": bb_dn, "bb_bw_20": bb_w,
                "atr_14": atr14,
            }

        f1d = self._fetch_1d_feature(symbol)
        if f1d:
            out["1d"]["features"] = f1d

        return out

    def load(self, symbols: List[str], one_hour_limit: int = 120) -> Dict[str, Dict[str, Any]]:
        return {s: self.load_symbol(s, one_hour_limit=one_hour_limit) for s in symbols}
```

---

# 3) 新建：`llm_trader/time_manager.py`

```python
# llm_trader/time_manager.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any
import datetime as dt
import pytz

ET = pytz.timezone("America/New_York")
UTC = pytz.UTC

@dataclass
class TimeManager:
    """
    交易时段管理：
    - --close-at "15:55" （美东时间）
    - --close-before 5    （收盘前 N 分钟关闭/清仓）
    - 收盘前禁止开新仓（--forbid-new-positions-before N）
    """
    close_at: Optional[str] = None          # "HH:MM" in ET
    close_before_min: Optional[int] = None  # minutes before 16:00 ET
    forbid_new_positions_before_min: int = 5

    def _today_close_et(self, now_et: dt.datetime) -> dt.datetime:
        return now_et.replace(hour=16, minute=0, second=0, microsecond=0)

    def _parse_close_at(self, now_et: dt.datetime) -> Optional[dt.datetime]:
        if not self.close_at:
            return None
        h, m = self.close_at.split(":")
        return now_et.replace(hour=int(h), minute=int(m), second=0, microsecond=0)

    def session_phase(self, now_utc: dt.datetime) -> str:
        """粗略分段：PRE (4:00-9:30), OPEN (9:30), RTH(9:30-15:59), CLOSE(16:00-16:15), POST(16:00-20:00)。"""
        now_et = now_utc.astimezone(ET)
        t = now_et.time()
        if t < dt.time(4, 0):
            return "OFF"
        if dt.time(4,0) <= t < dt.time(9,30):
            return "PRE"
        if t == dt.time(9,30):
            return "OPEN"
        if dt.time(9,30) < t < dt.time(16,0):
            return "RTH"
        if dt.time(16,0) <= t < dt.time(16,15):
            return "CLOSE"
        if dt.time(16,0) <= t < dt.time(20,0):
            return "POST"
        return "OFF"

    def should_flatten_now(self, now_utc: dt.datetime) -> bool:
        """是否立即触发清仓退出。优先级：close_at > close_before_min。"""
        now_et = now_utc.astimezone(ET)
        # close_at
        tgt = self._parse_close_at(now_et)
        if tgt and now_et >= tgt:
            return True
        # close_before_min
        if self.close_before_min is not None:
            close_et = self._today_close_et(now_et)
            if now_et >= (close_et - dt.timedelta(minutes=self.close_before_min)):
                return True
        return False

    def allow_new_position(self, now_utc: dt.datetime) -> bool:
        """收盘前 N 分钟禁开新仓；若 should_flatten_now=True 也不允许。"""
        if self.should_flatten_now(now_utc):
            return False
        now_et = now_utc.astimezone(ET)
        close_et = self._today_close_et(now_et)
        gate_from = close_et - dt.timedelta(minutes=max(0, self.forbid_new_positions_before_min))
        return now_et < gate_from

    def build_time_ctx(self, now_utc: dt.datetime) -> Dict[str, Any]:
        """供 Prompt 使用的时间上下文。"""
        now_et = now_utc.astimezone(ET)
        return {
            "now_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "now_et": now_et.strftime("%Y-%m-%d %H:%M:%S"),
            "phase": self.session_phase(now_utc),
            "should_flatten": self.should_flatten_now(now_utc),
            "allow_new_position": self.allow_new_position(now_utc),
            "forbid_new_positions_before_min": self.forbid_new_positions_before_min,
            "close_policy": {
                "close_at": self.close_at,
                "close_before_min": self.close_before_min
            }
        }
```

---

# 4) 新建：`llm_trader/prompt_builder_mtf.py`（DeepSeek-R1 版）

```python
# llm_trader/prompt_builder_mtf.py
from typing import Dict, Any, Optional
import json

def _short_bar_list(bars, k: int):
    out = []
    for b in bars[-k:]:
        out.append({k2: b[k2] for k2 in ("ts","open","high","low","close","volume") if k2 in b})
    return out

def build_prompt_for_r1(
    *,
    symbol: str,
    mtf_snapshot: Dict[str, Any],
    portfolio: Dict[str, Any],
    vix_value: Optional[float],
    time_ctx: Dict[str, Any],
    min_need: Dict[str, int],
    cash_limit: float,
    allow_new_position: bool,
    higher_tf_context: Optional[Dict[str, Any]] = None
) -> Dict[str, str]:
    """
    返回 {"system": ..., "user": ...}
    R1 要求：清晰的 step-by-step 约束 + 严格 JSON 输出。
    """
    # --- 精简 bars，控制 token ---
    mtf_view = {}
    for tf, pack in mtf_snapshot.items():
        bars = pack.get("bars", [])
        feats = pack.get("features", {})
        keep = 60 if tf in ("1s","5s") else 30 if tf in ("1m","5m") else 24
        mtf_view[tf] = {
            "bars": _short_bar_list(bars, keep),
            "features": feats
        }

    higher_view = higher_tf_context or {}
    # 最终可供模型消费的数据包（user message 用 JSON 串，让 R1 权衡）
    data_pack = {
        "symbol": symbol,
        "time": time_ctx,
        "market": {
            "vix": vix_value
        },
        "portfolio": portfolio,  # {position, avg_cost, cash, equity, pnl}
        "mtf": mtf_view,
        "higher_tf": higher_view,
        "constraints": {
            "cash_limit": cash_limit,
            "min_required_bars": min_need,
            "no_new_position": (not allow_new_position)
        }
    }

    system = (
        "You are an intraday trading decision agent. "
        "Always reason step-by-step *silently*, then output STRICT JSON ONLY. "
        "Respect hard gates: if `constraints.no_new_position=true`, do not open new positions. "
        "If `time.should_flatten=true`, decide to SELL existing long positions to flat."
    )

    # 让 R1 用“推理 + 结论”，但我们只接收结论 JSON
    user = f"""
You are given structured market context as JSON. Infer short-term vs medium-term regime, size risk by volatility/liquidity,
and decide an action. If no position exists and new positions are forbidden, you must HOLD.

Return STRICT JSON with fields:
{{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "shares": <int|null>,
  "rationale": <short string, one sentence>,
  "hard_gate": <"NO_NEW_POSITION"|"FLATTEN_EOD"|null>
}}

Rules:
- If time.should_flatten==true -> action MUST be "SELL" if position_size>0; set hard_gate="FLATTEN_EOD".
- If constraints.no_new_position==true and current position_size==0 -> action MUST be "HOLD"; set hard_gate="NO_NEW_POSITION".
- Sizing: propose `shares` notional <= available_cash and <= max single order = 10% of cash_limit.
- SELL means reduce/exit the current long position (we do not short in this mode).

DATA:
{json.dumps(data_pack, ensure_ascii=False)}
    """.strip()

    return {"system": system, "user": user}
```

---

# 5) 集成补丁：`run_single_bot.py`（参数 + 时序 + Prompt 注入 + 执行 Gate）

> 下面是**关键片段**，按注释在你的 `run_single_bot.py` 对应位置插入/替换即可。

```python
# --- [A] argparse 添加参数 ----------------------------------------------------
parser.add_argument("--higher-tf-refresh-sec", type=int, default=300,
                    help="刷新 1h/1d 背景快照的间隔（秒），默认 300s。")
parser.add_argument("--close-at", type=str, default=None,
                    help='美东时间固定清仓时刻，如 "15:55"。')
parser.add_argument("--close-before", type=int, default=None,
                    help="收盘前 N 分钟触发清仓（16:00- N 分钟）。")
parser.add_argument("--forbid-new-positions-before", type=int, default=5,
                    help="收盘前 N 分钟禁止开新仓，默认 5 分钟。")
```

```python
# --- [B] 初始化组件（创建 bot / main 前的依赖） -------------------------------
from llm_trader.realtime_aggregator import RealtimeAggregator
from llm_trader.higher_tf_context import HigherTFContextLoader
from llm_trader.time_manager import TimeManager, UTC
from llm_trader.prompt_builder_mtf import build_prompt_for_r1

aggregator = RealtimeAggregator()
higher_loader = HigherTFContextLoader(ch_client)     # 你的 ClickHouse client
higher_cache = {}
_last_higher_refresh = 0.0

tm = TimeManager(
    close_at=args.close_at,
    close_before_min=args.close_before,
    forbid_new_positions_before_min=args.forbid_new_positions_before
)
```

```python
# --- [C] 在交易循环里：刷新 higherTF 缓存 ------------------------------------
now_utc = dt.datetime.now(tz=UTC)
if time.time() - _last_higher_refresh >= args.higher_tf_refresh_sec:
    try:
        higher_cache = higher_loader.load(symbols, one_hour_limit=120)
    except Exception as e:
        logger.warning(f"higher-TF refresh failed: {e}")
    _last_higher_refresh = time.time()
```

```python
# --- [D] 每个 symbol 的决策：构建多周期视图 + 时间上下文 + Prompt ---------------
# 假设你已有 1s 实时数据（如通过 Redis SFS 取到最近若干 1s bars）：
bars_1s = await market_data_fetcher.get_1s_bars_from_redis(sym, n=60)  # 你原有的函数
for b in bars_1s:
    aggregator.update_from_1s(sym, {
        "ts": b["ts"], "open": b["open"], "high": b["high"],
        "low": b["low"], "close": b["close"], "volume": b.get("volume", 0.0)
    })

mtf_snapshot = aggregator.get_snapshot(sym)
time_ctx = tm.build_time_ctx(now_utc)
allow_new_position = time_ctx["allow_new_position"]

# 你的持仓/现金快照（示例字段）
portfolio_state = {
    "position_size": position_map.get(sym, 0),
    "avg_cost":      avg_cost_map.get(sym, 0.0),
    "cash":          cash_available,       # 你的计算
    "equity":        equity,               # 账户净值
    "pnl":           pnl_total
}

vix_value = vix_latest  # 若没有可传 None

prompt_pack = build_prompt_for_r1(
    symbol=sym,
    mtf_snapshot=mtf_snapshot,
    portfolio=portfolio_state,
    vix_value=vix_value,
    time_ctx=time_ctx,
    min_need={"1m": 20, "5m": 6},
    cash_limit=args.cash,
    allow_new_position=allow_new_position,
    higher_tf_context=higher_cache.get(sym)
)

# --- [E] 调用 LLM Provider ----------------------------------------------------
# 你的 provider 应该支持 system + user
result = await provider.decide(prompt_pack["system"], prompt_pack["user"])
# 期望 result 是 dict：{action, confidence, shares, hard_gate, ...}
action = result.get("action","HOLD")
confidence = float(result.get("confidence", 0.0))
shares = result.get("shares")
hard_gate = result.get("hard_gate")

# --- [F] 本地执行 Gate（双保险） ----------------------------------------------
# 1) 到达清仓时刻 → 有仓必卖
if time_ctx["should_flatten"] and portfolio_state["position_size"] > 0:
    action = "SELL"
    shares = portfolio_state["position_size"]

# 2) 禁开新仓：若无持仓，则屏蔽 BUY
if not allow_new_position and portfolio_state["position_size"] == 0:
    if action == "BUY":
        action = "HOLD"
        shares = None

# 3) 基本风控/边界（paper 阶段可轻）
if action == "BUY":
    # 不超过单笔上限（10%现金上限）与可用现金
    max_notional = args.cash * 0.10
    px = mtf_snapshot["1s"]["bars"][-1]["close"] if mtf_snapshot["1s"]["bars"] else None
    if px:
        cap_shares = max(0, int(min(max_notional, portfolio_state["cash"]) / px))
        if not shares or shares > cap_shares:
            shares = cap_shares
        if shares == 0:
            action = "HOLD"

if action == "SELL":
    if shares is None or shares <= 0 or shares > portfolio_state["position_size"]:
        shares = portfolio_state["position_size"]

# --- [G] 下单或模拟 ------------------------------------------------------------
if action in ("BUY","SELL") and shares and shares > 0:
    if DRY_RUN:
        logger.info(f"[{sym}] {action} x {shares} (simulated)")
    else:
        await broker.execute(sym, action, shares)  # 你现有的下单封装
else:
    logger.info(f"[{sym}] HOLD (conf={confidence:.2f}, gate={hard_gate})")
```

---

## 使用提示

* 若你不想改动 `market_data_fetcher.py`，就保留它原状，新增这 3 个模块（`realtime_aggregator.py / higher_tf_context.py / time_manager.py`）即可。
* 对 DeepSeek-R1 的 prompt，我们已把**收盘前关闸 / 清仓**的**硬规则**放进 system+user 双层约束，并在执行侧再做一次硬拦截（双保险）。
* `higher_tf_refresh_sec` 默认 5 分钟即可；若你只做超短决策，也可以拉长到 10~15 分钟。
* 如果某天 ClickHouse 的 `features_*` 没有盘中更新，也**不影响**实时决策；R1 将主要依赖 `RealtimeAggregator` 的 1s→多周期视图。

需要我帮你把这些补丁直接合到你当前的 `run_single_bot.py` 和 provider 里某段具体行号吗？我可以按「搜索锚点字符串」的方式给你**逐行替换版**，这样复制就能用。
你说得对：**Prompt 是核心，而且要“一套模板，多家模型可用”。**
下面给你一份**通用 Prompt 构建器**，适配 OpenAI、Claude、Gemini、DeepSeek（官方/英伟达 NIM），并内置了：

* 统一的**输出 JSON Schema（BUY/SELL/HOLD + shares + gate）**
* Provider 差异化设置（OpenAI/DeepSeek 走 `response_format`，Gemini 走 `response_mime_type`，Claude 强约束“只输出 JSON”）
* 多周期数据压缩（1s/5s/1m/5m/15m/30m/1h + 可选 1h/1d 背景）
* 风控硬闸门：EOD 清仓 / 收盘前禁止开新仓（在 Prompt 中显式说明）
* 自动“修复”无效 JSON 的二次提示（repair 提示）

把以下文件保存为：`llm_trader/prompt_builder_universal.py`
（与之前我给你的 `realtime_aggregator.py / higher_tf_context.py / time_manager.py` 完全兼容）

---

### `llm_trader/prompt_builder_universal.py`

```python
# llm_trader/prompt_builder_universal.py
from __future__ import annotations
from typing import Dict, Any, Optional, Tuple
import json
import math

# ---- 统一 JSON Schema（所有 LLM 输出必须匹配） -------------------------------
TRADE_JSON_SCHEMA = {
    "name": "trade_decision",
    "schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "shares": {"type": ["integer", "null"], "minimum": 0},
            "rationale": {"type": "string"},
            "hard_gate": {
                "type": ["string", "null"],
                "enum": ["NO_NEW_POSITION", "FLATTEN_EOD", None]
            }
        },
        "required": ["action", "confidence", "rationale"],
        "additionalProperties": False
    },
    "strict": True
}

def _short_bars(bars, keep: int) -> list:
    if not bars:
        return []
    rows = bars[-keep:]
    out = []
    for b in rows:
        out.append({
            k: b[k] for k in ("ts", "open", "high", "low", "close", "volume") if k in b
        })
    return out

def _compact_mtf(mtf_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    # 控制 token：短周期保留更多，长周期保留更少
    keep_map = {
        "1s": 90, "5s": 60, "1m": 40, "5m": 24, "15m": 16, "30m": 12, "1h": 12
    }
    out = {}
    for tf, pack in mtf_snapshot.items():
        bars = pack.get("bars") or []
        feats = pack.get("features") or {}
        keep = keep_map.get(tf, 12)
        out[tf] = {"bars": _short_bars(bars, keep), "features": feats}
    return out

def _truncate(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars] + "...(trunc)"

class UniversalPromptBuilder:
    """
    一套 Prompt，适配多家 LLM：
    - OpenAI（含 DeepSeek-NIM/OpenAI 兼容端点）：支持 response_format json_schema 或 json_object
    - DeepSeek Official：多数兼容 OpenAI，若不支持 json_schema，回退“只输出 JSON”的硬约束
    - Gemini：使用 generation_config.response_mime_type = application/json
    - Claude（Anthropic）：没有原生 json_schema，就用“必须输出 JSON”+ 严格格式说明

    产出：
      - messages: List[dict]  (system + user)
      - provider_kwargs: Dict[str, Any]  （各家专用的 response_format / generation_config 等）
    """
    def __init__(
        self,
        json_mode: str = "schema",   # "schema" | "object" | "none"
        style: str = "reasoning",    # "reasoning" | "concise"
        max_user_json_chars: int = 15000,  # 控制 data_pack JSON 体积
    ):
        self.json_mode = json_mode
        self.style = style
        self.max_user_json_chars = max_user_json_chars

    # ----------- 主构建函数 ----------------------------------------------------
    def build(
        self,
        *,
        provider: str,                       # "openai" | "anthropic" | "gemini" | "deepseek_official" | "deepseek_nvidia"
        symbol: str,
        mtf_snapshot: Dict[str, Any],
        portfolio: Dict[str, Any],
        time_ctx: Dict[str, Any],
        cash_limit: float,
        allow_new_position: bool,
        vix_value: Optional[float] = None,
        higher_tf_context: Optional[Dict[str, Any]] = None,
        sizing_cap_pct: float = 0.10,        # 单笔不超过 cash_limit 的比例
        forbid_short: bool = True,           # 当前模式仅做多：SELL 仅指减仓/清仓
        language: str = "en"                 # "en" | "zh"
    ) -> Tuple[list, dict]:
        # 1) 压缩多周期数据
        mtf_view = _compact_mtf(mtf_snapshot)
        higher_view = higher_tf_context or {}

        # 2) 组装信息包（给 LLM 消费）
        data_pack = {
            "symbol": symbol,
            "time": time_ctx,
            "market": {"vix": vix_value},
            "portfolio": portfolio,  # {position_size, avg_cost, cash, equity, pnl}
            "mtf": mtf_view,
            "higher_tf": higher_view,
            "constraints": {
                "cash_limit": cash_limit,
                "max_single_order_ratio": sizing_cap_pct,
                "no_new_position": (not allow_new_position),
                "forbid_short": forbid_short
            }
        }
        data_json = json.dumps(data_pack, ensure_ascii=False)
        data_json = _truncate(data_json, self.max_user_json_chars)

        # 3) 语言/措辞
        if language == "zh":
            instr_header = (
                "你是一个日内交易决策智能体。先在心里逐步推理，但**只输出 JSON**。"
                "务必遵守硬规则：若 `constraints.no_new_position=true` 且当前无仓，则必须 HOLD；"
                "若 `time.should_flatten=true` 且持有多头，则必须 SELL 至空仓。"
            )
            sizing_rules = (
                f"- 下单规模：单笔名义金额 ≤ 账户限额的 {int(sizing_cap_pct*100)}%，且 ≤ available_cash。\n"
                "- 当前模式不做空：SELL 仅表示减仓/清仓。\n"
            )
            task = (
                "给定结构化的多周期数据（1s~1h）与 1h/1d 背景、仓位与会话时间，"
                "判断短期/中期方向与波动风险，输出 BUY/SELL/HOLD、confidence、shares、rationale。"
            )
            fmt_rules = (
                "严格以**单个 JSON 对象**输出，字段如下：\n"
                "{\n"
                '  "action": "BUY" | "SELL" | "HOLD",\n'
                "  \"confidence\": 0.0-1.0,\n"
                "  \"shares\": <int|null>,\n"
                "  \"rationale\": <简短一句话>,\n"
                '  "hard_gate": "NO_NEW_POSITION"|"FLATTEN_EOD"|null\n'
                "}\n"
                "若 `time.should_flatten=true` 且有仓：action 必须为 SELL，hard_gate=FLATTEN_EOD；\n"
                "若 `constraints.no_new_position=true` 且无仓：action 必须为 HOLD，hard_gate=NO_NEW_POSITION。"
            )
        else:
            instr_header = (
                "You are an intraday trading decision agent. Think step-by-step silently, "
                "but output JSON only. Respect hard gates: if `constraints.no_new_position=true` and "
                "position_size==0 -> HOLD. If `time.should_flatten=true` and position_size>0 -> SELL to flat."
            )
            sizing_rules = (
                f"- Sizing: notional per order ≤ {int(sizing_cap_pct*100)}% of cash_limit and ≤ available_cash.\n"
                "- No shorting in this mode: SELL means reduce/exit existing long.\n"
            )
            task = (
                "Given structured multi-timeframe data (1s~1h), 1h/1d context, portfolio and session time, "
                "infer short-vs-medium-term regime and risk, then output BUY/SELL/HOLD, confidence, shares, rationale."
            )
            fmt_rules = (
                "Return a SINGLE JSON object:\n"
                "{\n"
                '  "action": "BUY" | "SELL" | "HOLD",\n'
                "  \"confidence\": 0.0-1.0,\n"
                "  \"shares\": <int|null>,\n"
                "  \"rationale\": <one short sentence>,\n"
                '  "hard_gate": "NO_NEW_POSITION"|"FLATTEN_EOD"|null\n'
                "}\n"
                "If `time.should_flatten=true` & position_size>0 -> SELL and hard_gate=FLATTEN_EOD.\n"
                "If `constraints.no_new_position=true` & position_size==0 -> HOLD and hard_gate=NO_NEW_POSITION."
            )

        # 4) system / user
        system = instr_header
        if self.style == "reasoning":
            system += " Do all reasoning internally and never print it."

        user = (
            f"{task}\n\n"
            "Rules:\n"
            f"{sizing_rules}"
            f"{fmt_rules}\n\n"
            f"DATA:\n{data_json}"
        )

        # 5) 各 provider 的 json/输出约束参数
        provider_kwargs: Dict[str, Any] = {}
        p = provider.lower()

        if p in ("openai", "deepseek_nvidia"):
            # OpenAI & 兼容：优先 json_schema；若端点不支持，可退化到 json_object。
            if self.json_mode == "schema":
                provider_kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": TRADE_JSON_SCHEMA
                }
            elif self.json_mode == "object":
                provider_kwargs["response_format"] = {"type": "json_object"}
            # else: none -> rely on prompt

        elif p in ("deepseek_official", "deepseek"):
            # 多数 DeepSeek Official 端点兼容 OpenAI；若不支持 json_schema，保留纯提示约束
            if self.json_mode == "object":
                provider_kwargs["response_format"] = {"type": "json_object"}

        elif p == "gemini":
            # Gemini：使用 response_mime_type=application/json
            provider_kwargs["generation_config"] = {"response_mime_type": "application/json"}

        elif p in ("anthropic", "claude"):
            # Claude：没有 json_schema，可使用 tool schema（取决于你的 SDK）。
            # 这里统一用提示硬约束；你的 provider 里也可用 tools API 来强约束。
            pass

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ]
        return messages, provider_kwargs

    # ----------- JSON 修复提示（当 LLM 返回非法 JSON 时二次追问） ---------------
    def repair_messages(self, bad_text: str, language: str = "en") -> Tuple[list, dict]:
        if language == "zh":
            repair_user = (
                "你上一次的输出不是合法 JSON 或没有匹配指定 schema。"
                "请**仅返回**一个 JSON 对象，不要包含任何多余文本、代码块或解释。"
                f"\n必须匹配如下 JSON Schema（简述）：{json.dumps(TRADE_JSON_SCHEMA['schema'], ensure_ascii=False)}"
                f"\n上次原文：\n{_truncate(bad_text, 4000)}"
            )
        else:
            repair_user = (
                "Your previous output was not valid JSON or did not match the required schema. "
                "Return ONLY a single JSON object, with no extra text or markdown. "
                f"\nSchema (summary): {json.dumps(TRADE_JSON_SCHEMA['schema'])}"
                f"\nPrevious raw text:\n{_truncate(bad_text, 4000)}"
            )
        messages = [{"role": "user", "content": repair_user}]
        return messages, {}
```

---

## 如何接到你现有的 provider

你已有 `openai_provider.py / gemini_provider.py / deepseek_provider.py / deepseek_nvidia_provider.py`。
在调用 LLM 决策的地方（你现在传 `system, user`），改为：

```python
from llm_trader.prompt_builder_universal import UniversalPromptBuilder

builder = UniversalPromptBuilder(
    json_mode="schema",       # OpenAI/DeepSeek-NIM 走 json_schema；不支持就换 "object"
    style="reasoning",        # R1/Claude 可用 "reasoning"，fast 模型可用 "concise"
)

messages, provider_kwargs = builder.build(
    provider=provider_name,           # "openai" | "anthropic" | "gemini" | "deepseek_nvidia" | "deepseek_official"
    symbol=sym,
    mtf_snapshot=mtf_snapshot,        # 来自 RealtimeAggregator.get_snapshot(sym)
    portfolio=portfolio_state,        # 你的持仓/现金
    time_ctx=time_ctx,                # 来自 TimeManager.build_time_ctx(now_utc)
    cash_limit=args.cash,
    allow_new_position=time_ctx["allow_new_position"],
    vix_value=vix_value,
    higher_tf_context=higher_cache.get(sym),
    sizing_cap_pct=0.10,
    forbid_short=True,
    language="zh" if args.lang == "zh" else "en",
)

# 调 provider：把 provider_kwargs 传进去（各文件各自处理）
result = await provider.decide(messages=messages, **provider_kwargs)

# 如果 JSON 解析失败：
if result is None or isinstance(result, str):  # 你的解析逻辑
    repair_msgs, repair_kwargs = builder.repair_messages(str(result), language="zh")
    result = await provider.decide(messages=repair_msgs, **repair_kwargs)
```

> 注：各 provider 内部请分别读取 `provider_kwargs`：
>
> * OpenAI/DeepSeek-NIM：把 `provider_kwargs.get("response_format")` 作为 `response_format` 传入 Chat Completions。
> * Gemini：把 `provider_kwargs["generation_config"]` 合到对应 SDK 的生成配置里（`response_mime_type="application/json"`）。
> * Claude：没有 json_schema 就只用 messages，保持“只输出 JSON”的强约束语气。

---

## 这套 Prompt 的要点

* **硬规则双重化**：Prompt 明确“禁止开新仓 / EOD 清仓”语义；执行侧仍做二次 gate（双保险）。
* **统一 JSON Schema**：所有模型输出结构一致，方便解析与下单。
* **多周期视角**：短周期（1s/5s/1m）权重高，长周期（5m/15m/30m/1h）做趋势/波动；可选 1h/1d 背景锚定。
* **自适应大小**：自动裁剪 bars 与 JSON 长度，避免超 token。
* **跨模型风格**：reasoning/concise 两种语气，R1/Claude 适合 reasoning，fast 模型选 concise。
* **JSON 修复通道**：一键重试，专治 JSON 不合规。

如果你愿意，我可以再把**各 provider 的 `decide()` 函数如何取用 `provider_kwargs`**写成具体补丁段，直接贴到你的 `*_provider.py` 中。
👌 明白。接下来把“通用 Prompt 构建器”真正落地到你现有的四个 provider 文件里。下面是**最小必需补丁**：每个 provider 的 `decide()` 都能读取我给你的 `UniversalPromptBuilder` 返回的 `provider_kwargs`（比如 `response_format` / `generation_config`），并**严格返回已解析好的 JSON dict**（失败就把原文抛回去给外层做 repair）。

---

# 打补丁要点

* 统一入口：`async def decide(self, *, messages: list, **provider_kwargs) -> dict | str`
* OpenAI & DeepSeek-NIM：传入 `response_format`（优先 json_schema，不支持则回退）
* DeepSeek Official：多数走 OpenAI 兼容；不支持 schema 就回退
* Gemini：把 `generation_config={"response_mime_type": "application/json"}` 合并到调用
* 所有 provider：**只返回** `dict`（成功解析的交易 JSON）或 `str`（原始文本，给外层 repair 重试）

---

## 1) `openai_provider.py` 补丁

```python
# openai_provider.py
import os, json, asyncio
from typing import Any, Dict
from openai import OpenAI

class OpenAIProvider:
    def __init__(self, model: str = None, api_key: str = None, base_url: str = None, timeout: int = 60):
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.client = OpenAI(
            api_key=api_key or os.getenv("OPENAI_API_KEY"),
            base_url=base_url or os.getenv("OPENAI_BASE_URL") or None,
            timeout=timeout
        )

    async def decide(self, *, messages: list, **provider_kwargs) -> Dict[str, Any] | str:
        """
        provider_kwargs 可能包含:
          - response_format: {"type": "json_schema", "json_schema": {...}} | {"type": "json_object"}
        """
        def _call():
            try:
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.2,
                    response_format=provider_kwargs.get("response_format")
                )
            except Exception:
                # 回退：有些 OpenAI 兼容端点不支持 response_format
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.2
                )

        resp = await asyncio.to_thread(_call)
        text = (resp.choices[0].message.content or "").strip()
        try:
            return json.loads(text)
        except Exception:
            return text  # 外层会用 repair 再试
```

---

## 2) `deepseek_nvidia_provider.py`（NIM，OpenAI 兼容）补丁

```python
# deepseek_nvidia_provider.py
import os, json, asyncio
from typing import Any, Dict
from openai import OpenAI

class DeepSeekNvidiaProvider:
    """
    通过 NVIDIA NIM(OpenAI-compatible) 调用 DeepSeek-R1：
    环境变量：
      NIM_API_KEY
      NIM_BASE_URL (例如 https://integrate.api.nvidia.com/v1)
    """
    def __init__(self, model: str = None, api_key: str = None, base_url: str = None, timeout: int = 120):
        self.model = model or os.getenv("DEEPSEEK_NVIDIA_MODEL", "deepseek-ai/deepseek-r1")
        self.client = OpenAI(
            api_key=api_key or os.getenv("NIM_API_KEY"),
            base_url=base_url or os.getenv("NIM_BASE_URL"),
            timeout=timeout
        )

    async def decide(self, *, messages: list, **provider_kwargs) -> Dict[str, Any] | str:
        def _call():
            try:
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.2,
                    # R1 常见: 支持 json_object；若不支持 schema 会报错
                    response_format=provider_kwargs.get("response_format")
                )
            except Exception:
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.2
                )

        resp = await asyncio.to_thread(_call)
        text = (resp.choices[0].message.content or "").strip()
        try:
            return json.loads(text)
        except Exception:
            # R1 可能含“思维链”片段，尝试截取最后一个 {} 块；失败则交给外层 repair
            try:
                start = text.rfind("{")
                end = text.rfind("}")
                if start != -1 and end != -1 and end > start:
                    return json.loads(text[start:end+1])
            except Exception:
                pass
            return text
```

---

## 3) `deepseek_provider.py`（官方 API，OpenAI 兼容为主）补丁

```python
# deepseek_provider.py
import os, json, asyncio
from typing import Any, Dict
from openai import OpenAI

class DeepSeekOfficialProvider:
    """
    DeepSeek 官方（通常 OpenAI 兼容）：
      DEEPSEEK_API_KEY
      DEEPSEEK_BASE_URL = https://api.deepseek.com (如官方变动，请在 .env 设置)
    """
    def __init__(self, model: str = None, api_key: str = None, base_url: str = None, timeout: int = 90):
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-reasoner")
        self.client = OpenAI(
            api_key=api_key or os.getenv("DEEPSEEK_API_KEY"),
            base_url=base_url or os.getenv("DEEPSEEK_BASE_URL"),
            timeout=timeout
        )

    async def decide(self, *, messages: list, **provider_kwargs) -> Dict[str, Any] | str:
        def _call():
            try:
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.2,
                    response_format=provider_kwargs.get("response_format")
                )
            except Exception:
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.2
                )

        resp = await asyncio.to_thread(_call)
        text = (resp.choices[0].message.content or "").strip()
        try:
            return json.loads(text)
        except Exception:
            # 和 NIM 同样的兜底
            try:
                start = text.rfind("{")
                end = text.rfind("}")
                if start != -1 and end != -1 and end > start:
                    return json.loads(text[start:end+1])
            except Exception:
                pass
            return text
```

---

## 4) `gemini_provider.py` 补丁（Google Generative AI）

```python
# gemini_provider.py
import os, json, asyncio
from typing import Any, Dict
import google.generativeai as genai

class GeminiProvider:
    def __init__(self, model: str = None, api_key: str = None):
        self.model_name = model or os.getenv("GEMINI_MODEL", "gemini-1.5-pro")
        genai.configure(api_key=api_key or os.getenv("GEMINI_API_KEY"))
        # system 指令用 system_instruction，user 合并为 prompt
        self.model = genai.GenerativeModel(self.model_name)

    async def decide(self, *, messages: list, **provider_kwargs) -> Dict[str, Any] | str:
        """
        provider_kwargs 可能包含:
          - generation_config={"response_mime_type": "application/json"}
        """
        # 拆 system/user → Gemini 风格输入
        system_texts = [m["content"] for m in messages if m.get("role") == "system"]
        user_texts   = [m["content"] for m in messages if m.get("role") == "user"]

        system_instruction = "\n\n".join(system_texts) if system_texts else None
        user_prompt = "\n\n".join(user_texts) if user_texts else ""

        gen_cfg = provider_kwargs.get("generation_config", {}) or {}
        if "response_mime_type" not in gen_cfg:
            # 缺省强制 JSON 输出
            gen_cfg["response_mime_type"] = "application/json"

        def _call():
            mdl = self.model
            if system_instruction:
                mdl = genai.GenerativeModel(self.model_name, system_instruction=system_instruction)
            return mdl.generate_content(
                user_prompt,
                generation_config=gen_cfg
            )

        resp = await asyncio.to_thread(_call)
        text = (resp.text or "").strip()
        try:
            return json.loads(text)
        except Exception:
            # Gemini 偶尔会在前后加说明性文字，简单截括号
            try:
                start = text.find("{")
                end = text.rfind("}")
                if start != -1 and end != -1 and end > start:
                    return json.loads(text[start:end+1])
            except Exception:
                pass
            return text
```

---

## 在调用处如何使用

```python
from llm_trader.prompt_builder_universal import UniversalPromptBuilder

builder = UniversalPromptBuilder(json_mode="schema", style="reasoning")
messages, provider_kwargs = builder.build(
    provider=provider_name,              # "openai"|"gemini"|"deepseek_nvidia"|"deepseek_official"
    symbol=symbol,
    mtf_snapshot=mtf_snapshot,           # RealtimeAggregator.get_snapshot()
    portfolio=portfolio_state,           # position/cash/equity/pnl
    time_ctx=time_ctx,                   # TimeManager.build_time_ctx(now_utc): 含 should_flatten/allow_new_position
    cash_limit=args.cash,
    allow_new_position=time_ctx["allow_new_position"],
    vix_value=vix_value,
    higher_tf_context=higher_cache.get(symbol),
    sizing_cap_pct=0.10,
    forbid_short=True,
    language="zh"
)

decision = await provider.decide(messages=messages, **provider_kwargs)

if not isinstance(decision, dict):
    # 二次修复（统一的 repair 提示）
    repair_msgs, repair_kwargs = builder.repair_messages(str(decision), language="zh")
    decision = await provider.decide(messages=repair_msgs, **repair_kwargs)
```

---

## 小结（你得到的能力）

* 一套 Prompt 同时跑：OpenAI / Gemini / DeepSeek(官方+NIM)
* **统一 JSON 返回**，解析失败自动 repair
* 支持 R1 等“推理型”模型（更长响应时间也 OK），也支持 fast 模型
* 与你已有的时间/仓位硬闸门一致，**Prompt 与执行层双保险**

如果你愿意，我可以继续把 `run_single_bot.py` 里对 **EOD 闭市禁开仓 + 到点强平** 的 gating 与这套 Prompt 的 `time_ctx` 串起来（你之前给的 `close-at` / `close-before` 需求）。


很好的问题。你有 16 GB 可用内存，完全能把实时多周期+LLM 跑起来，但要**刻意做“内存友好”设计**。下面给你一套可直接落地的思路与配额建议（不写代码，只讲策略与参数）。

---

# TL;DR（一页结论）

* **数据结构**用“环形缓冲 + 数值型数组”，别用“list of dict / 大量 DataFrame”。
* **滚动特征**全部改成**在线/增量算法**（EMA/RSI/方差/布林/ATR 等均可 O(1) 更新），只保存窗口内必要值（float32）。
* **多周期**实时部分保留到 15 m 即可；更长周期（1h/1d）做**轻量级摘要**（昨日范围/ATR/关键位）而不是全量序列。
* **LLM 提供数据**严格“瘦身”：Top-K 特征 + 小样本指标；历史“记忆”用**压缩的交易摘要**，而非完整对话/原文。
* **并发与缓存**：限制活跃交易标的数 N_active（例如 5–20），其余只做粗筛；日志落盘+滚动切片，禁用大内存 Handler。
* **守护与退化**：到达内存阈值时自动丢弃最低优先级缓存/降采样/暂停新增标的。

---

# 1) 预算怎么估

以**环形结构 + 结构化数组**为例（每条 bar 存 `ts:int64 + 6~8 个 float32/int32` ≈ **40–48 B** / bar）：

* 1s bars，一天 RTH 约 23,400 条/标的
  → ~1.0–1.2 MB/标的
* 5s/1m/5m/15m 都是 1s 的聚合，**无需重复存全量**，只保留最近若干根：

  * 5s：最近 12–60 根（~0.5–2 KB/标的）
  * 1m：最近 60–120 根（~3–6 KB/标的）
  * 5m：最近 24 根（~1–2 KB/标的）
  * 15m：最近 8 根（<1 KB/标的）

> 结论：**核心占用在 1s 环形缓冲**。
> 100 个标的 ≈ 100–150 MB；再加上特征环形缓存（float32）与元数据，总体**< 500 MB** 完全可控。真正吃内存的是**Python 对象开销/错误的数据结构/日志与缓存**，不是数值数组。

---

# 2) 数据结构与滚动特征（大头优化点）

### 用这些，直接减 10× 内存

* **环形缓冲**（固定大小 deque / 自定义 ring index）+ **NumPy/Polars/PyArrow** 数组；
  避免：`list[dict]`、每周期新建 DataFrame、频繁拼接字符串大文本等。
* **数值类型**一律 `float32 / int32`；时间戳用 `int64 epoch_ms` 或 `numpy.datetime64[ns]`。
  避免：默认 float64、Python 的 `datetime`（对象开销大）。
* **结构化数组**（NumPy structured dtype / Arrow Table）：一条记录是平铺字段，cache 友好。

### 在线/增量算法（O(1) 空间）

* **EMA/DEMA/KAMA/RSI/ATR/DI/ADX**：全部可以递推；
* **布林/滚动方差/标准差**：用**滑窗和“增删”**维护 `sum` / `sum2`（或 Welford + 环形窗口）；
* **VWAP / 成交量均值**：维护累计值 & 窗口和；
* **Z-Score/Skew/Kurt**：用滑窗 Moment 递推（保留 M1..M4 与窗口 ring 缓存若干值）；
* **相关/β**：小窗（20/60）用并行维护 `Σx, Σy, Σxy, Σx², Σy²`。

> 规则：**每个指标只保留当下值 + 为递推必要的常数个状态**；滑窗只存需要“减去”的那一点（通常一个 ring）。
> 这样**每个指标 ~几十字节**，几十个指标也不过**几 KB/标的**。

---

# 3) 多周期与“全局视角”的内存友好法

* **实时周期**：1s → 5s → 1m → 5m → 15m（都在内存增量聚合），每个周期只保留**短窗**（上面建议）。
* **更大周期**（1h/4h/1d）：**不给全序列**，只给 LLM **摘要**：

  * 昨日范围（High/Low/Close）、昨日 ATR、昨日 VWAP、关键支撑/阻力（分位/Donchian）。
  * 1d 的**趋势标签**（上升/盘整/下行）、波动 regime（ATR/σ 分位）。
  * 这些摘要以 **几十个数值**表示即可，内存≈零。
* **冷启动**：用 ClickHouse 拉**短片**（例如最近 15 分钟的 1s、昨天的 1d 摘要）；拉完就**丢内存外历史**，只留下“摘要+实时环”。
* **动态标的集**：维护**活跃集合 N_active**（5–20）——

  * 入选条件：成交额/波动/信号强度排名；
  * 非活跃标的只保留极少的元信息（价格/成交额/最新信号），随时“拉进来”。

---

# 4) LLM 侧的“瘦身”策略（省内存+省钱）

* **Prompt 严格定长**：

  * 每周期仅传 **Top-K（10–20）核心特征** + **多周期小窗口**（如 1s 60 根、5s 12 根、1m 10 根、5m 6 根、15m 4 根）；
  * 历史“记忆”只传**交易结果摘要**（P&L 变化、近 N 次胜率、近 N 次平均盈亏/回撤、最近 1–2 条关键复盘建议）。
* **数值压缩**：

  * 小数保留 3–4 位；用 **bps/百分点** 表示偏差；
  * 把时间段聚合成**区间起止**而非逐条列出。
* **对话记忆外置**：

  * 供“学习用”的长文本（反思/复盘）**落盘 SQLite/ClickHouse**，每次只带**最新摘要**进入 prompt。
* **多模型策略**：

  * 高频周期用**快模型**（mini/flash）；
  * 关键再平衡/大仓位前切给**R1/思考型模型**，但**低频触发**（例如每 5–10 分钟或出现 regime 变化时）。

---

# 5) 日志与缓存（最容易被忽视的内存杀手）

* **日志**：只用 `RotatingFileHandler`/`TimedRotatingFileHandler`，禁用内存 Handler。
  设置 `maxBytes=10–50MB, backupCount=5–10`；调试日志放到**单独文件**，默认 INFO。
* **结果缓存**：

  * LRU + TTL（比如 trades/指标/行情片段），“弱引用”或“只存 ID”；
  * 大对象（如 LLM 原文）**直接落盘**（gzip）而不是内存常驻。
* **字符串构造**：Prompt 用 `io.StringIO`/模板拼接；**避免**把 DataFrame 转巨大字符串再切片。
* **并发**：

  * 用 `asyncio.Semaphore` 控制**同时在跑的 symbol 数量**与**同时在路上的 LLM 调用**；
  * 每个任务结束**马上 `del` 中间产物**，大循环尾部**偶尔 `gc.collect()`**（例如每 200 个 cycle 一次）。

---

# 6) 动态退化与熔断（防止“瞬时爆内存”）

* **硬阈值**：设定进程 RSS（例如 10 GB）和软阈值（8 GB）。超过软阈值：

  1. 降低活跃标的数量；
  2. 砍半 1s 环形窗口长度（例如 23,400 → 6,000）；
  3. 暂停 R1/大 prompt，只用 fast 模型；
  4. 缩短 LLM 输入（只保留 Top-10 特征+1m/5m 小窗）；
  5. 清理历史缓存与调试日志。
* **超过硬阈值**：

  * 立即**禁止新开仓**，仅允许平仓；
  * 落盘当前状态并**有序退出**（防止系统级 OOM）。

---

# 7) 推荐参数（可直接抄）

* **环形窗口**（每标的）

  * 1s：6,000–12,000（≈ 10–20 分钟），float32；
  * 5s：60（≈ 5 分钟）；1m：120（≈ 2 小时）；5m：36（≈ 3 小时）；15m：16（≈ 4 小时）。
* **活跃标的**：N_active = 10（可按总内存线性放大/缩小）。
* **特征集合**：≤ 32 个（核心价量/波动/与 VWAP 偏离/RSI/EMA 组/市场因子）+ 4–6 个“交易记忆”指标。
* **数据类型**：价格/指标 `float32`；计数 `int32`；时间 `int64`（epoch ms）。
* **LLM**：

  * 高频：mini/flash，输入 token ≤ 1.5–2k；
  * 低频复盘/大仓位：R1，输入 token ≤ 3–4k（严格上限）。

---

# 8) 监控与排查

* **实时内存监控**：`psutil.Process(os.getpid()).memory_info().rss`，每个 cycle 记录；
* **阈值事件**：打印“退化等级”日志，附当前缓存大小与活跃标的列表；
* **离线排查**：`tracemalloc`/`memray`/`memory_profiler` 按周期采样，定位漏引用。

---

## 最后一条建议

先开**保守档**（小窗口、小活跃集、瘦 prompt），稳定后逐步放宽。
把“**环形缓冲 + 在线指标**”作为制度化约束：**任何新增特征，如需用到“过去 N 根”，必须先给出 O(1) 递推与 O(1) 状态大小的实现**，否则不准进主干。这样你在 16 GB 空间里，跑几十个标的 + 多 provider + 实时聚合，依然稳如老狗。
