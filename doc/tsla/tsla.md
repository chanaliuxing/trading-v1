太好了，我把 gemini 的提示和你现有的代码/DDL 全部“对齐重排”，给你一份**专门为你系统量身定制**的落地方案。目标是：不大改现网、先把 `tsla_v7_2.py` 能稳定吃到数据跑起来，再顺手把其它 7 个策略也统一到同一取数层。

---

# 0) 范围与原则（不跑偏）

* **只做“取数链路”的落地**：Bars（1m/5m）、L1（Bid/Ask）、VIX（1m）→ 策略统一入口。
* **最小改动**：不重写 Engine，不引入新依赖；优先复用你已有的 `market_data_facade.py` + `strategy_engine.py`。
* **统一口径**：所有策略只读 `trading.bars_{res}_current` 和 `lake.quotes_l1`；VIX 优先真 VIX，缺失自动回退 VIXY。
* **触发节拍**：以“**新 1m bar 收盘**”为唯一节拍；轮询 ClickHouse **每 3 秒**检查是否出现新 1m bar（不要每 5 秒强跑）。

---

# 1) 策略所需数据 → 你的表与主题的映射

| 需求                 | 你系统中的来源                                              | 说明                                         |
| ------------------ | ---------------------------------------------------- | ------------------------------------------ |
| TSLA 1m / 5m OHLCV | `trading.bars_1m_current`, `trading.bars_5m_current` | 已有/按我们 P2 方案创建；来源 IBKR 或自聚合都可，通过视图统一       |
| TSLA L1（bid/ask）   | `lake.quotes_l1`                                     | 由 `lake_writer` 消费 quotes 主题落库（若未跑，立刻起一实例） |
| SPY / QQQ 5m       | `trading.bars_5m_current`                            | 做市场 regime                                 |
| VIX 1m（优先）         | `trading.bars_1m_current`（symbol='VIX'）              | 从 IBKR 指数合约拉取，Kafka→Writer→CH              |
| VIXY 1m（备用）        | `trading.bars_1m_current`（symbol='VIXY'）             | 当没有 VIX 订阅时回退                              |

> 若 1m/5m 的分表/视图还没齐：按你仓库里的 `clickhouse_view.md` / `ch_init.sql` 建好 `bars_{res}_ibkr` / `bars_{res}_agg` 与 `bars_{res}_current`（我们前面已给过样例）。

---

# 2) **第一优先**：把 VIX 1m 喂进来（IBKR → Kafka → CH）

## 2.1 合约与订阅校验（只做一次）

* 正确合约：**指数**（不是股票），**交易所 CBOE 或 CBOEIND**
  `Index('VIX', 'CBOE', 'USD')`（或 `'CBOEIND'`）
* 你之前报错 `No security definition…` 就是因为用成了 `Stock('VIX','SMART','USD')`

**动作**

* 放 `tools/vix_check.py`（我已给代码），运行并确认能拿到 1m bars。
* 若 TWS 里是 “Delayed”，去 Client Portal 开通与指数相关的数据包。
* 在没开通前，**策略先用 VIXY 兜底**（下面会自动回退，不影响上线节奏）。

## 2.2 在线抓取进 Kafka（常驻）

* 放 `services/ibkr/vix_ingestor.py`（我已给代码），每 ~55s 拉一次“最近 1 天 1m”，只发**最新一根**到 `md.bar.1m.ibkr`
* 你已有 `services/market/writer/ch_writer.py` 正在消费这个主题，落 `trading.bars_1m_ibkr` → 由 `bars_1m_current` 暴露给上游
* 验证 SQL：

  ```sql
  SELECT max(ts_start) FROM trading.bars_1m_ibkr WHERE symbol='VIX';
  SELECT max(ts_start) FROM trading.bars_1m_current WHERE symbol='VIX';
  ```
* 无需修改 writer：我们沿用 `{data, metadata.ts_us}` 契约与 `ReplacingMergeTree(ver)` 幂等。

> **注意**：不要旁路直插 CH，一律走 Kafka→Writer，避免幂等/优先级冲突。

---

# 3) **统一策略取数**：扩展 `market_data_facade.py`（推荐）

> 你已经有 `market_data_facade.py` —— 直接在这里补全 4 个方法，所有策略复用，不再各自拼 SQL/L1。

### 3.1 需要新增/确认的方法签名（Facade 层）

```python
class MarketDataFacade:
    # 1) 单标的最近 N 根 bars（1m/5m）
    def bars(self, symbol: str, res: str, limit: int) -> List[Dict]:
        """
        SELECT ts_start AS ts, open, high, low, close, volume
        FROM trading.bars_{res}_current
        WHERE symbol = %(s)s
        ORDER BY ts DESC
        LIMIT %(n)s
        # 返回：按 ts 升序（旧→新），便于策略直接滚动指标
        """

    # 2) L1：asof 前 N 秒窗口内最新一条 bid/ask
    def last_quote(self, symbol: str, asof: Optional[datetime]=None, staleness_sec: int=5) -> Dict[str, Optional[float]]:
        """
        SELECT argMax(bid, ts) AS bid, argMax(ask, ts) AS ask
        FROM lake.quotes_l1
        WHERE symbol=%(s)s AND ts <= %(t)s AND ts > %(t)s - INTERVAL %(sec)s SECOND
        """

    # 3) VIX 别名：有 VIX 用 VIX，否则 VIXY
    def vix_symbol(self) -> str:
        """
        SELECT 1 FROM trading.bars_1m_current WHERE symbol='VIX' LIMIT 1
        -> 'VIX' or 'VIXY'
        """

    # 4) 轻量订阅（轮询模式）：检测是否出现新 bar
    async def poll_new_bar(self, symbol: str, res: str, poll_sec: int, on_new_bar: Callable[[Dict], Awaitable[None]]):
        """
        每 poll_sec 秒取最近2根；若最新 ts 变大→回调 on_new_bar(row_dict)
        """
```

### 3.2 SQL 要点

* Bars 来源统一：`trading.bars_{res}_current`
* L1 的 staleness（默认 5 秒），避免拿到陈旧报价
* 返回的 bars **按时间升序**，策略不必 reverse

---

# 4) **对接 Engine**：在 `strategy_engine.py` 接入 Facade（不改策略）

> 你的基类/引擎文件：`strategy_engine.py`、`strategy_base.py`、`engine.py` 结构我已看过；我们只在 Engine 层补两条“对外接口实现”，策略无需改动。

### 4.1 在 Engine 里用 Facade 实现策略期望的两个方法

**history_features(symbol, bar_type, limit)**

* 映射 `bar_type` → 分辨率：`60 -> '1m'`, `300 -> '5m'`（或直接传 `'1m'/'5m'`）
* `symbol == 'VIX'` → 替换成 `facade.vix_symbol()`
* 返回 `facade.bars(sym, res, limit)`

**subscribe_features(strategy_id, symbols, bar_sec)**

* 推断分辨率：`bar_sec <= 60 → '1m'`，否则 `'5m'`
* 设置轮询频率：`1m→3s`，`5m→10s`
* 为每个 symbol 启一个 `facade.poll_new_bar(sym,res, poll, callback)`
* **在回调前**，若 symbol 是“交易股票”（TSLA/你的其它7只），再拼上 `facade.last_quote(symbol)` 的 `bid/ask` 字段，统一投喂给策略的 `on_features`

> 这就是我们之前“EngineAdapter”的思想，直接并入你现有 Engine/Facade，不新建一层，避免重复。

---

# 5) `tsla_v7_2.py` 的接线方式（零改策略）

* 在你现在启动策略的入口（runner 或 `main.py`）里：

  1. 构造策略实例：`s = TslaV7_2(...)`
  2. **把 Engine 里的 facade 注入**（具体看你 Engine 的依赖注入方式，通常是构造时传入或属性赋值）
  3. 调用策略原有的 `initialize` / `start`（你已有模板，照旧）

* 策略内部所有 `await self.engine.history_features(...)`、`await self.engine.subscribe_features(...)` 都会走我们刚才在 Engine 上的新实现，读取 CH 与 L1，而不是自己整合。

---

# 6) 配置 & 运行节拍（`config.yml` 追加）

```yaml
engine_adapter:         # 若你的 config 没这个段落，可放在 engine: 下
  quote_staleness_sec: 5
  poll_sec_1m: 3
  poll_sec_5m: 10
  vix_alias: "VIXY"     # 若无 VIX 时回退
```

---

# 7) 验收步骤（一次性打通）

**Step A：CH 数据就绪**

```sql
-- TSLA/你的其它股票近1天是否有1m
SELECT symbol, count()
FROM trading.bars_1m_current
WHERE symbol IN ('TSLA','AAPL','MSFT','NVDA') AND ts_start>=today()-1
GROUP BY symbol;

-- L1 是否在流入
SELECT symbol, max(ts) FROM lake.quotes_l1 WHERE symbol='TSLA' GROUP BY symbol;
```

**Step B：VIX 就绪（或 VIXY 回退）**

```sql
SELECT max(ts_start) FROM trading.bars_1m_current WHERE symbol IN ('VIX','VIXY');
```

**Step C：Runner 冒烟（可用我上次给的 `tools/runner_tsla_smoke.py`）**

* 预期：每分钟打印一次 TSLA 的新 1m bar（带 bid/ask），SPY/QQQ/VIX(VIXY) 只打印 OHLCV
* 证明“**节拍 = 新 1m bar 收盘**”，不是每 5 秒瞎跑

**Step D：在交易时段做一小时灰度**

* 只启 TSLA 策略，观察日志/告警
* 看 `ClickHouse` 数据是否平稳滚动，无缺口；看 L1 拼接是否正常（`bid <= ask`）
* 确认 EOD 平仓逻辑触发正常（15:57）

---

# 8) 变更清单（谁改什么、改到哪）

1. **VIX 抓取（新增）**

   * 文件：`services/ibkr/vix_ingestor.py`（已提供）
   * 部署：systemd 或你的服务管理
   * 依赖：`ib-insync`（若未装）

2. **Facade 扩展（小改）**

   * 文件：`market_data_facade.py`
   * 新增方法：`bars / last_quote / vix_symbol / poll_new_bar`

3. **Engine 对接（小改）**

   * 文件：`strategy_engine.py`（或你实际的 engine 文件）
   * 用 Facade 实现 `history_features` / `subscribe_features`，并在回调前给“交易股票”追加 `bid/ask`

4. **配置（小改）**

   * 文件：`config.yml`
   * 追加 `engine_adapter.*` 的轮询与回退参数（或合并进你现有 engine 段落）

> 以上四处改动，**不触碰**你的 `ch_writer.py / backfill_service.py / reconciler.py / five_sec_agg.py / fifteen_sec_agg.py` 等现有链路；后者保持原样。

---

# 9) 时间线（两步跑通）

* **Day 0（今天）**
  A. 跑 `vix_check.py`；能通就起 `vix_ingestor`，不能通先不耽误后续（VIXY 回退）
  B. 改 `market_data_facade.py` + `strategy_engine.py`（各添几十行，1 次提交）
  C. 跑 `runner_tsla_smoke.py` 看每分钟节拍 → OK

* **Day 1（明天盘前）**
  A. 用 TSLA 策略灰度 1 小时；观察日志、下单限价是否用到 L1、异常熔断是否正常
  B. 逐步把其它 7 只策略也注入同一 Engine/Fascade（无需动策略内部）

---

# 10) 失败与回滚

* **Facade/Engine 改动引起错误**：

  * 直接回滚这两文件到上一个 commit；策略会回到旧的取数方式（如果旧方式还能跑）
* **VIX 抓取异常**：

  * 先停 `vix_ingestor`，`vix_symbol()` 会回退到 `VIXY`，策略不中断
* **数据缺口**：

  * 交给你已部署的小回补/大回补；不要让策略自行补数据，以免口径不一

---

## 一句话总结

* **先把 VIX 喂进来**（或自动回退 VIXY），**再把 Engine 与 Facade 对上**——策略端零改；
* **单一口径**：Bars 统一读 `bars_{res}_current`，L1 统一读 `lake.quotes_l1`；
* **单一节拍**：只在新 1m bar 出现时触发（轮询 CH 每 3 秒）。

如果你点头，我可以把 **`market_data_facade.py` 和 `strategy_engine.py` 的具体修改段落（精确到函数体内容）** 写出来，直接贴进去就行。


收到！我们就按“**从数据入口打通→策略吃到数据→慢慢扩展**”来落地。下面给你的是**专为你当前仓库结构**准备的最小改动实现：直接把代码贴进相应文件就能跑；如果你本地的 DB 封装有细微不同，只改 2–3 行 import/调用即可。

---

# 0) 一次明确“今天要完成的事”

1. 让 **VIX 1m** 真正进库（IBKR → Kafka → ClickHouse）。
2. 让所有策略（先从 `tsla_v7_2.py`）**通过统一接口拿数据**：

   * Bars：`trading.bars_{res}_current`（1m/5m）
   * L1：`lake.quotes_l1`（bid/ask）
   * VIX：优先 `VIX`，没开权限时自动回退 `VIXY`
3. 策略**以“新 1 分钟 K 线出现”为节拍**触发（引擎每 3 秒轮询一次 CH 检测新 bar，不要每 5 秒强跑）。

---

# 1) 配置：加上 Engine 轮询 & VIX 回退

把这段追加到你的 `config.yml`（或放到 engine 段下也行）：

```yaml
engine_adapter:
  quote_staleness_sec: 5   # L1 允许的最大陈旧时间（秒）
  poll_sec_1m: 3           # 1m 轮询频率
  poll_sec_5m: 10          # 5m 轮询频率
  vix_alias: "VIXY"        # 没有 VIX 时自动使用 VIXY
```

---

# 2) VIX 上线（IBKR → Kafka → ClickHouse）

## 2.1 一次性合约/订阅检查（确保能拿到 VIX 1m）

新建 `tools/vix_check.py`：

```python
# tools/vix_check.py
from ib_insync import IB, Index

ib = IB()
ib.connect('127.0.0.1', 7497, clientId=7001)  # 改成你的 TWS/Gateway

for ex in ("CBOE", "CBOEIND"):
    vix = Index('VIX', ex, 'USD')
    cds = ib.reqContractDetails(vix)
    if cds:
        print("OK: VIX on", ex, "conId=", cds[0].contract.conId)
        ib.qualifyContracts(vix)
        bars = ib.reqHistoricalData(
            vix, endDateTime='', durationStr='2 D',
            barSizeSetting='1 min', whatToShow='MIDPOINT',
            useRTH=True, formatDate=2
        )
        print("bars:", len(bars), "last:", bars[-1] if bars else None)
        break
else:
    print("无法找到 VIX 指数合约（请在 Client Portal 开通指数/CBOE 数据包，或先用 VIXY 兜底）")

ib.disconnect()
```

## 2.2 常驻抓取：`services/ibkr/vix_ingestor.py`

（每 ~55s 拉一次“最近 1 天 1m”，只发**最新一根**）

```python
# services/ibkr/vix_ingestor.py
import asyncio, json, time
from datetime import datetime, timezone
from aiokafka import AIOKafkaProducer
from ib_insync import IB, Index

KAFKA_BOOTSTRAP = "localhost:9092"   # 改成你的
TOPIC = "md.bar.1m.ibkr"             # 你的 ch_writer 正在消费的主题

def to_msg(bar):
    ts = datetime.fromisoformat(str(bar.date).replace("Z","+00:00")).replace(tzinfo=timezone.utc)
    return {
        "data": {
            "symbol": "VIX",
            "res": "1m",
            "ts_start": ts.isoformat().replace("+00:00","Z"),
            "ts_end":   ts.isoformat().replace("+00:00","Z"),
            "open":  float(bar.open),
            "high":  float(bar.high),
            "low":   float(bar.low),
            "close": float(bar.close),
            "volume": int(getattr(bar, "volume", 0) or 0),
            "is_backfill": 0,
            "source": "IBKR"
        },
        "metadata": { "ts_us": int(time.time()*1_000_000) }
    }

async def main():
    ib = IB()
    ib.connect('127.0.0.1', 7497, clientId=7102)  # 改成你的
    for ex in ("CBOE", "CBOEIND"):
        vix = Index('VIX', ex, 'USD')
        if ib.reqContractDetails(vix):
            break

    prod = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        key_serializer=lambda v: v.encode("utf-8"),
        linger_ms=5, acks="all", retries=3
    )
    await prod.start()
    last_iso = None
    try:
        while True:
            bars = ib.reqHistoricalData(
                vix, endDateTime='', durationStr='1 D',
                barSizeSetting='1 min', whatToShow='MIDPOINT',
                useRTH=True, formatDate=2, keepUpToDate=False, timeout=30
            )
            if bars:
                b = bars[-1]
                iso = str(b.date)
                if iso != last_iso and b.close not in (None, 0.0):
                    await prod.send_and_wait(TOPIC, key="VIX", value=to_msg(b))
                    last_iso = iso
            await asyncio.sleep(55)
    finally:
        await prod.stop()
        ib.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
```

> 你的 `services/market/writer/ch_writer.py` 会把 `md.bar.1m.ibkr` 写入 `trading.bars_1m_ibkr` → `bars_1m_current`。
> 验证：

```sql
SELECT max(ts_start) FROM trading.bars_1m_ibkr  WHERE symbol='VIX';
SELECT max(ts_start) FROM trading.bars_1m_current WHERE symbol='VIX';
```

> 若暂时拿不到 VIX（权限未开），**继续做后面的步骤**，策略会自动用 `VIXY` 回退，不阻塞上线。

---

# 3) 统一取数：扩展 `market_data_facade.py`

在你现有的 `market_data_facade.py` 里**新增**下列方法（保留你原有代码）：

```python
# market_data_facade.py
from __future__ import annotations
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Callable, Awaitable, Iterable

# 兼容你项目里的 ClickHouse 封装：优先用自有封装，缺省用 clickhouse_connect
try:
    from libs.db.clickhouse import ClickHouseDB
    _CH = ClickHouseDB()
    def ch_query(sql: str, params: dict | None = None):
        return _CH.query(sql, params or {})
except Exception:
    from clickhouse_connect import get_client
    _client = get_client(host='localhost', username='default', password='')
    def ch_query(sql: str, params: dict | None = None):
        # 简单替换 %(x)s 为 {x}，按你项目需要可以移除
        if params:
            for k, v in params.items():
                sql = sql.replace(f"%({k})s", f"'{v}'" if not isinstance(v, (int,float)) else str(v))
        return [dict(row) for row in _client.query(sql).result_rows]

class MarketDataFacade:
    def __init__(self, quote_staleness_sec: int = 5, vix_alias: str = "VIXY"):
        self.quote_staleness_sec = int(quote_staleness_sec)
        self.vix_alias = vix_alias

    def _tbl(self, res: str) -> str:
        return f"trading.bars_{res}_current"

    # 1) Bars（升序返回，旧→新）
    def bars(self, symbol: str, res: str, limit: int) -> List[Dict[str, Any]]:
        sql = f"""
        SELECT ts_start AS ts, open, high, low, close, volume
        FROM {self._tbl(res)}
        WHERE symbol = %(s)s
        ORDER BY ts DESC
        LIMIT %(n)s
        """
        rows = ch_query(sql, {"s": symbol, "n": int(limit)}) or []
        return list(reversed(rows))

    # 2) 最近报价（默认5秒内）
    def last_quote(self, symbol: str, asof: Optional[datetime] = None) -> Dict[str, Optional[float]]:
        asof = asof or datetime.now(timezone.utc)
        sql = """
        SELECT argMax(bid, ts) AS bid, argMax(ask, ts) AS ask
        FROM lake.quotes_l1
        WHERE symbol=%(s)s AND ts <= %(t)s AND ts > %(t)s - INTERVAL %(sec)s SECOND
        """
        r = (ch_query(sql, {"s": symbol, "t": asof, "sec": self.quote_staleness_sec}) or [{}])[0]
        return {"bid": r.get("bid"), "ask": r.get("ask")}

    # 3) VIX 别名回退
    def vix_symbol(self) -> str:
        has = ch_query("SELECT 1 FROM trading.bars_1m_current WHERE symbol='VIX' LIMIT 1")
        return "VIX" if has else self.vix_alias

    # 4) 轻量订阅（轮询检测新bar）
    async def poll_new_bar(self, symbol: str, res: str, poll_sec: int,
                           on_new_bar: Callable[[Dict[str, Any]], Awaitable[None]]):
        last_ts: Optional[datetime] = None
        while True:
            try:
                rows = self.bars(symbol, res, 2)
                if rows:
                    cur = rows[-1]
                    ts = cur["ts"]
                    if (last_ts is None) or (ts > last_ts):
                        await on_new_bar(cur)
                        last_ts = ts
            except Exception as e:
                # 你可以换成你项目里的 logger
                print("poll_new_bar.error", symbol, res, e)
            await asyncio.sleep(poll_sec)
```

---

# 4) 引擎对接：在 `strategy_engine.py` 实现策略期望的两个接口

> 不改 `tsla_v7_2.py`；只在引擎里“接线”到 Facade。以下代码写法尽量**贴近你现有文件风格**，如有不同命名，请照抄函数体逻辑即可。

```python
# strategy_engine.py （示意：在你的 Engine 类里增加/改造这两个方法）
import asyncio
from libs.config import config
from market_data_facade import MarketDataFacade

class StrategyEngine:
    def __init__(self, ...):
        ...
        staleness = int(config.get("engine_adapter.quote_staleness_sec", 5))
        vix_alias = str(config.get("engine_adapter.vix_alias", "VIXY"))
        self.md = MarketDataFacade(staleness, vix_alias)
        self._tasks = []

    async def history_features(self, symbol: str, bar_type: str, limit: int):
        # bar_type 兼容 "1m"/"5m" 和 60/300
        res = {"60":"1m","300":"5m","1m":"1m","5m":"5m"}.get(str(bar_type), str(bar_type))
        sym = self.md.vix_symbol() if symbol == "VIX" else symbol
        return self.md.bars(sym, res, limit)

    async def subscribe_features(self, strategy_id: str, symbols: list[str], bar_sec: int):
        res  = "1m" if int(bar_sec) <= 60 else "5m"
        poll = int(config.get(f"engine_adapter.poll_sec_{res}", 3 if res=="1m" else 10))
        for s in symbols:
            sym = self.md.vix_symbol() if s == "VIX" else s
            self._tasks.append(asyncio.create_task(self._poll_symbol(strategy_id, sym, res, poll)))

    async def _poll_symbol(self, strategy_id: str, symbol: str, res: str, poll_sec: int):
        async def _emit(bar_row: dict):
            feat = dict(bar_row)
            # 只有交易标的（股票）拼 L1；指数/ETF 不强制（SPY/QQQ/VIX/VIXY）
            if symbol not in ("SPY","QQQ","VIX","VIXY"):
                feat.update(self.md.last_quote(symbol))
            # 调用策略 on_features（按你项目实际的回调方式来）
            await self._on_features(strategy_id, symbol, feat)

        await self.md.poll_new_bar(symbol, res, poll_sec, _emit)

    # 这个方法根据你现有框架实际名称来，比如 engine 会分发到策略实例：
    async def _on_features(self, strategy_id: str, symbol: str, features: dict):
        strategy = self._get_strategy_by_id(strategy_id)  # 你已有的方法
        await strategy.on_features(symbol, features)
```

> 以上只是在 **Engine 层接线**，不碰你的策略逻辑和下单通道。
> 以后 7 个策略也全部复用这一层，策略里不用再自己整合 bars/L1。

---

# 5) 启动验证（最小闭环）

1. **VIX 合约检查**

```bash
python tools/vix_check.py
```

2. **VIX 抓取（前台跑 2 分钟）**

```bash
python -m services.ibkr.vix_ingestor
```

查询：

```sql
SELECT max(ts_start) FROM trading.bars_1m_current WHERE symbol IN ('VIX','VIXY');
```

3. **策略冒烟（不下单，只看节拍）**
   如果你愿意，用我之前给的 `tools/runner_tsla_smoke.py`；或者直接用你现有的 runner 启动 `tsla_v7_2.py`。期望现象：

   * 初始化 `history_features('TSLA','1m', 400)` 取到 400 根；
   * 每出现新 1m bar（通常在分钟边界后 1–3 秒），`on_features('TSLA', {..., bid, ask})` 被调用一次；
   * SPY/QQQ/VIX 只传 OHLCV 即可。

---

## 常见坑（再强调）

* **VIX 报错 200**：请务必用 `Index('VIX','CBOE','USD')`（或 `CBOEIND`），不是股票也不是 SMART。TWS 面板能否实时显示 VIX 是最直观的订阅验证。
* **不要每 5 秒跑信号**：以“**新 1m bar 出现**”作为唯一节拍；引擎每 3 秒轮询一次 ClickHouse 即可。
* **没有 VIX 也能继续**：`vix_symbol()` 自动回退 `VIXY`，不耽误策略上线；等权限开好，起 `vix_ingestor` 即自动切回真实 VIX，无需改策略。

---

# 下一步（你确认 OK 后，我再给）

* 我可以把 **“TSLA 最小 Runner（带下单占位）”** 和 **systemd 服务单**（`vix_ingestor.service`）一起发你，直接常驻部署。
* 如果你愿意，把 `market_data_facade.py` 和 `strategy_engine.py` 的当前版本简要贴一段（类名/方法名），我就给你**逐行替换版 patch**，保证拷贝即用。

随时说“上”就行。

太好了，那我就直接给你“可拷贝即用”的落地包：**两处小改 + 两个新文件 + 一个最小 Runner + 一个 systemd 服务单 + 自检清单**。你照着做就能把 `tsla_v7_2.py` 跑起来（并且以后 7 个策略都用同一层拿数据）。

---

# 1) 配置补充（一次性）

在 `config.yml` 里追加（若已有同名项，保留你的值即可）：

```yaml
engine_adapter:
  quote_staleness_sec: 5
  poll_sec_1m: 3
  poll_sec_5m: 10
  vix_alias: "VIXY"
```

---

# 2) 新增：VIX 合约检查脚本（一次性验证）

`tools/vix_check.py`

```python
from ib_insync import IB, Index

ib = IB()
ib.connect('127.0.0.1', 7497, clientId=7001)  # 改成你的TWS/Gateway

for ex in ("CBOE", "CBOEIND"):
    vix = Index('VIX', ex, 'USD')
    cds = ib.reqContractDetails(vix)
    if cds:
        print("OK: VIX on", ex, "conId=", cds[0].contract.conId)
        ib.qualifyContracts(vix)
        bars = ib.reqHistoricalData(
            vix, endDateTime='', durationStr='2 D',
            barSizeSetting='1 min', whatToShow='MIDPOINT',
            useRTH=True, formatDate=2
        )
        print("bars:", len(bars), "last:", bars[-1] if bars else None)
        break
else:
    print("无法找到VIX指数合约（请在Client Portal开通指数/CBOE数据包，或先用VIXY兜底）")

ib.disconnect()
```

运行一次：

```bash
python tools/vix_check.py
```

---

# 3) 新增：VIX 抓取 → Kafka（常驻）

`services/ibkr/vix_ingestor.py`

```python
import asyncio, json, time
from datetime import datetime, timezone
from aiokafka import AIOKafkaProducer
from ib_insync import IB, Index

KAFKA_BOOTSTRAP = "localhost:9092"   # 改成你的
TOPIC = "md.bar.1m.ibkr"             # 你的 ch_writer 正在消费的主题

def to_msg(bar):
    ts = datetime.fromisoformat(str(bar.date).replace("Z","+00:00")).replace(tzinfo=timezone.utc)
    return {
        "data": {
            "symbol": "VIX",
            "res": "1m",
            "ts_start": ts.isoformat().replace("+00:00","Z"),
            "ts_end":   ts.isoformat().replace("+00:00","Z"),
            "open":  float(bar.open),
            "high":  float(bar.high),
            "low":   float(bar.low),
            "close": float(bar.close),
            "volume": int(getattr(bar, "volume", 0) or 0),
            "is_backfill": 0,
            "source": "IBKR"
        },
        "metadata": { "ts_us": int(time.time()*1_000_000) }
    }

async def main():
    ib = IB()
    ib.connect('127.0.0.1', 7497, clientId=7102)  # 改
    for ex in ("CBOE", "CBOEIND"):
        vix = Index('VIX', ex, 'USD')
        if ib.reqContractDetails(vix):
            break

    prod = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        key_serializer=lambda v: v.encode("utf-8"),
        linger_ms=5, acks="all", retries=3
    )
    await prod.start()
    last_iso = None
    try:
        while True:
            bars = ib.reqHistoricalData(
                vix, endDateTime='', durationStr='1 D',
                barSizeSetting='1 min', whatToShow='MIDPOINT',
                useRTH=True, formatDate=2, keepUpToDate=False, timeout=30
            )
            if bars:
                b = bars[-1]
                iso = str(b.date)
                if iso != last_iso and b.close not in (None, 0.0):
                    await prod.send_and_wait(TOPIC, key="VIX", value=to_msg(b))
                    last_iso = iso
            await asyncio.sleep(55)
    finally:
        await prod.stop()
        ib.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
```

前台试跑 2 分钟：

```bash
python -m services.ibkr.vix_ingestor
```

CH 验证：

```sql
SELECT max(ts_start) FROM trading.bars_1m_ibkr  WHERE symbol='VIX';
SELECT max(ts_start) FROM trading.bars_1m_current WHERE symbol='VIX';
```

---

# 4) 小改 ①：`market_data_facade.py`（统一取数）

在文件中加入以下方法（不删你的原有代码）：

```python
# --- 新增 import ---
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Callable, Awaitable

# --- 在 MarketDataFacade 类里新增 ---
def _tbl(self, res: str) -> str:
    return f"trading.bars_{res}_current"

def bars(self, symbol: str, res: str, limit: int) -> List[Dict[str, Any]]:
    sql = f"""
    SELECT ts_start AS ts, open, high, low, close, volume
    FROM {self._tbl(res)}
    WHERE symbol = %(s)s
    ORDER BY ts DESC
    LIMIT %(n)s
    """
    rows = self._ch.query(sql, {"s": symbol, "n": int(limit)})  # 如果你封装名不同，替换这行
    rows = rows or []
    return list(reversed(rows))

def last_quote(self, symbol: str, asof: Optional[datetime] = None) -> Dict[str, Optional[float]]:
    asof = asof or datetime.now(timezone.utc)
    sql = """
    SELECT argMax(bid, ts) AS bid, argMax(ask, ts) AS ask
    FROM lake.quotes_l1
    WHERE symbol=%(s)s AND ts <= %(t)s AND ts > %(t)s - INTERVAL %(sec)s SECOND
    """
    r = (self._ch.query(sql, {"s": symbol, "t": asof, "sec": int(self.config.get("engine_adapter.quote_staleness_sec", 5))}) or [{}])[0]
    return {"bid": r.get("bid"), "ask": r.get("ask")}

def vix_symbol(self) -> str:
    has = self._ch.query("SELECT 1 FROM trading.bars_1m_current WHERE symbol='VIX' LIMIT 1")
    return "VIX" if has else str(self.config.get("engine_adapter.vix_alias", "VIXY"))

async def poll_new_bar(self, symbol: str, res: str, poll_sec: int,
                       on_new_bar: Callable[[Dict[str, Any]], Awaitable[None]]):
    last_ts: Optional[datetime] = None
    while True:
        try:
            rows = self.bars(symbol, res, 2)
            if rows:
                cur = rows[-1]
                ts = cur["ts"]
                if (last_ts is None) or (ts > last_ts):
                    await on_new_bar(cur)
                    last_ts = ts
        except Exception as e:
            # TODO: 换成你的 logger
            print("poll_new_bar.error", symbol, res, e)
        await asyncio.sleep(poll_sec)
```

> 注意：上面用了 `self._ch.query(...)` 与 `self.config.get(...)` 的写法，与你当前 Facade 的依赖注入可能略有差异。若你的封装类名/方法名不同，只要把 **执行 SQL 的那行** 和 **读配置的那行** 对应替换即可。

---

# 5) 小改 ②：`strategy_engine.py`（接线给策略）

在 Engine 类中**新增/修改**以下 3 个方法（函数体照抄即可；类名/获得策略实例的方式按你的文件适配）：

```python
import asyncio
from libs.config import config
from market_data_facade import MarketDataFacade

class StrategyEngine:
    def __init__(self, *args, **kwargs):
        ...
        self.md = MarketDataFacade()
        self._tasks = []

    async def history_features(self, symbol: str, bar_type: str, limit: int):
        res = {"60":"1m","300":"5m","1m":"1m","5m":"5m"}.get(str(bar_type), str(bar_type))
        sym = self.md.vix_symbol() if symbol == "VIX" else symbol
        return self.md.bars(sym, res, limit)

    async def subscribe_features(self, strategy_id: str, symbols: list[str], bar_sec: int):
        res  = "1m" if int(bar_sec) <= 60 else "5m"
        poll = int(config.get(f"engine_adapter.poll_sec_{res}", 3 if res=="1m" else 10))
        for s in symbols:
            sym = self.md.vix_symbol() if s == "VIX" else s
            self._tasks.append(asyncio.create_task(self._poll_symbol(strategy_id, sym, res, poll)))

    async def _poll_symbol(self, strategy_id: str, symbol: str, res: str, poll_sec: int):
        async def _emit(bar_row: dict):
            feat = dict(bar_row)
            if symbol not in ("SPY","QQQ","VIX","VIXY"):  # 仅对交易标的拼L1
                feat.update(self.md.last_quote(symbol))
            await self._on_features(strategy_id, symbol, feat)  # 分发给策略

        await self.md.poll_new_bar(symbol, res, poll_sec, _emit)
```

> 你的 Engine 里如果 `on_features` 的分发方法名不同，把 `self._on_features(...)` 替换成你已有的分发调用即可。

---

# 6) 最小 Runner（只打印节拍，确认每分钟一次）

`tools/runner_tsla_smoke.py`

```python
import asyncio
from strategy_engine import StrategyEngine

class DummyTsla:
    def __init__(self, engine: StrategyEngine):
        self.engine = engine

    async def on_features(self, symbol, feat):
        print(f"[{symbol}] {feat['ts']} close={feat['close']} "
              f"{'bid='+str(feat.get('bid'))+' ask='+str(feat.get('ask')) if symbol=='TSLA' else ''}")

    async def start(self):
        hist = await self.engine.history_features('TSLA', '1m', 400)
        print("loaded bars:", len(hist), "last:", hist[-1]['ts'] if hist else None)
        await self.engine.subscribe_features("tsla_smoke", ['TSLA','SPY','QQQ','VIX'], 60)

async def main():
    eng = StrategyEngine()
    s = DummyTsla(eng)
    await s.start()
    while True:
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
```

启动：

```bash
python -m tools.runner_tsla_smoke
```

预期输出：每分钟 1 次 TSLA（带 bid/ask），SPY/QQQ/VIX 只 OHLCV。

---

# 7) systemd 服务单（让 VIX 抓取常驻）

`/etc/systemd/system/vix_ingestor.service`

```ini
[Unit]
Description=IBKR VIX 1m Ingestor -> Kafka
After=network.target

[Service]
User=ubuntu                 # 改
WorkingDirectory=/path/to/your/repo   # 改
ExecStart=/usr/bin/python -m services.ibkr.vix_ingestor
Restart=always
RestartSec=5
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vix_ingestor
journalctl -u vix_ingestor -f
```

---

# 8) 自检清单（按顺序）

1. **CH 有 1m Bars 与 L1**

   ```sql
   SELECT symbol, count() FROM trading.bars_1m_current
   WHERE symbol IN ('TSLA','AAPL') AND ts_start >= today()-1 GROUP BY symbol;

   SELECT symbol, max(ts) FROM lake.quotes_l1
   WHERE symbol='TSLA' GROUP BY symbol;
   ```
2. **VIX 或 VIXY 可用**

   ```sql
   SELECT symbol, max(ts_start) FROM trading.bars_1m_current
   WHERE symbol IN ('VIX','VIXY') GROUP BY symbol;
   ```
3. **Runner 节拍**

   * `runner_tsla_smoke` 每分钟打印一次，基本在分钟边界后 1–3 秒
4. **策略接线**

   * 把 `tsla_v7_2.py` 的 `self.engine` 指向你刚才改好的 `StrategyEngine`（它已经实现了 `history_features` / `subscribe_features`）

---

# 9) 常见问题提示

* **VIX 报错 200**：你用错了合约（Stock/SMART）。请用 `Index('VIX','CBOE','USD')` 或 `CBOEIND`。TWS 面板要能实时显示 VIX。
* **没有 VIX 也能跑**：`vix_alias=VIXY` 自动兜底，不阻塞策略上线。
* **不要“每5秒跑一次策略”**：我们是以**新1m bar出现**为节拍（Engine 每 3 秒轮询 CH 检测），这更稳也与回补一致。

---

做到这里，你的 `tsla_v7_2.py` 就能在**统一数据口径**上跑起来了。
确认无误后，我可以再给你一份“多策略 Runner 模板 + 批量注入 Engine 的示例”，把另外 7 个策略一次性接上，不需要改它们内部逻辑。


完美👌 那我直接给你**可提交的改动包**（diff 形式）+ 一把就能跑通的命令清单。照抄→提交→跑测试，就把 `tsla_v7_2.py` 接上统一数据口径了。

---

# 1) 改动包（统一 diff）

## A) `market_data_facade.py`：新增统一取数方法（bars/L1/VIX别名/轮询）

```diff
*** a/market_data_facade.py
--- b/market_data_facade.py
@@
-from datetime import datetime
-from typing import Any
+from datetime import datetime, timezone
+from typing import List, Dict, Any, Optional, Callable, Awaitable
@@
 class MarketDataFacade:
     def __init__(self, *args, **kwargs):
         ...
         # 确保这里能拿到配置/CH 连接（按你项目原有写法保留）
         self.config = kwargs.get("config", getattr(self, "config", {}))
         self._ch = kwargs.get("ch", getattr(self, "_ch", None))
+        # 兜底常量
+        self._quote_staleness = int(self.config.get("engine_adapter.quote_staleness_sec", 5))
+        self._vix_alias = str(self.config.get("engine_adapter.vix_alias", "VIXY"))
+
+    # --- 新增：内部表名助手 ---
+    def _tbl(self, res: str) -> str:
+        return f"trading.bars_{res}_current"
+
+    # --- 新增：Bars（升序返回，旧→新）---
+    def bars(self, symbol: str, res: str, limit: int) -> List[Dict[str, Any]]:
+        sql = f"""
+        SELECT ts_start AS ts, open, high, low, close, volume
+        FROM {self._tbl(res)}
+        WHERE symbol = %(s)s
+        ORDER BY ts DESC
+        LIMIT %(n)s
+        """
+        rows = self._ch.query(sql, {"s": symbol, "n": int(limit)}) or []
+        return list(reversed(rows))
+
+    # --- 新增：最近 L1 报价（默认允许 5 秒陈旧）---
+    def last_quote(self, symbol: str, asof: Optional[datetime] = None) -> Dict[str, Optional[float]]:
+        asof = asof or datetime.now(timezone.utc)
+        sql = """
+        SELECT argMax(bid, ts) AS bid, argMax(ask, ts) AS ask
+        FROM lake.quotes_l1
+        WHERE symbol=%(s)s AND ts <= %(t)s AND ts > %(t)s - INTERVAL %(sec)s SECOND
+        """
+        r = (self._ch.query(sql, {"s": symbol, "t": asof, "sec": self._quote_staleness}) or [{}])[0]
+        return {"bid": r.get("bid"), "ask": r.get("ask")}
+
+    # --- 新增：VIX 别名回退（优先 VIX，缺失则用 VIXY）---
+    def vix_symbol(self) -> str:
+        has = self._ch.query("SELECT 1 FROM trading.bars_1m_current WHERE symbol='VIX' LIMIT 1")
+        return "VIX" if has else self._vix_alias
+
+    # --- 新增：轻量订阅（轮询新 bar）---
+    async def poll_new_bar(self, symbol: str, res: str, poll_sec: int,
+                           on_new_bar: Callable[[Dict[str, Any]], Awaitable[None]]):
+        last_ts: Optional[datetime] = None
+        while True:
+            try:
+                rows = self.bars(symbol, res, 2)
+                if rows:
+                    cur = rows[-1]
+                    ts = cur["ts"]
+                    if (last_ts is None) or (ts > last_ts):
+                        await on_new_bar(cur)
+                        last_ts = ts
+            except Exception as e:
+                # TODO: 换成你项目里的日志
+                print("poll_new_bar.error", symbol, res, e)
+            await asyncio.sleep(poll_sec)
```

## B) `strategy_engine.py`：实现策略期望接口，喂 L1，按“新1m bar”节拍触发

```diff
*** a/strategy_engine.py
--- b/strategy_engine.py
@@
-from datetime import datetime
+from datetime import datetime
+import asyncio
+from libs.config import config
+from market_data_facade import MarketDataFacade
@@
 class StrategyEngine:
     def __init__(self, *args, **kwargs):
         ...
+        self.md = MarketDataFacade(config=getattr(self, "config", config), ch=getattr(self, "_ch", None))
+        self._tasks = []
 
+    # --- 新增：历史数据提供给策略初始化（指标回填等）---
+    async def history_features(self, symbol: str, bar_type: str, limit: int):
+        res = {"60":"1m","300":"5m","1m":"1m","5m":"5m"}.get(str(bar_type), str(bar_type))
+        sym = self.md.vix_symbol() if symbol == "VIX" else symbol
+        return self.md.bars(sym, res, limit)
+
+    # --- 新增：订阅（轮询 CH，发现新 bar 就回调策略 on_features）---
+    async def subscribe_features(self, strategy_id: str, symbols: list[str], bar_sec: int):
+        res  = "1m" if int(bar_sec) <= 60 else "5m"
+        poll = int(config.get(f"engine_adapter.poll_sec_{res}", 3 if res=="1m" else 10))
+        for s in symbols:
+            sym = self.md.vix_symbol() if s == "VIX" else s
+            self._tasks.append(asyncio.create_task(self._poll_symbol(strategy_id, sym, res, poll)))
+
+    async def _poll_symbol(self, strategy_id: str, symbol: str, res: str, poll_sec: int):
+        async def _emit(bar_row: dict):
+            feat = dict(bar_row)
+            # 仅对交易标的拼 L1（指数/ETF 不强制）
+            if symbol not in ("SPY","QQQ","VIX","VIXY"):
+                feat.update(self.md.last_quote(symbol))
+            await self._on_features(strategy_id, symbol, feat)  # 分发给策略（保持你原有实现）
+
+        await self.md.poll_new_bar(symbol, res, poll_sec, _emit)
```

## C) 新增：VIX 合约检查与抓取 → Kafka（发往你的 writer 主题）

### `tools/vix_check.py`（一次性验证）

```python
from ib_insync import IB, Index

ib = IB(); ib.connect('127.0.0.1', 7497, clientId=7001)  # 改成你的
for ex in ("CBOE", "CBOEIND"):
    vix = Index('VIX', ex, 'USD')
    cds = ib.reqContractDetails(vix)
    if cds:
        print("OK: VIX on", ex, "conId=", cds[0].contract.conId)
        ib.qualifyContracts(vix)
        bars = ib.reqHistoricalData(vix, endDateTime='', durationStr='2 D',
                                    barSizeSetting='1 min', whatToShow='MIDPOINT',
                                    useRTH=True, formatDate=2)
        print("bars:", len(bars), "last:", bars[-1] if bars else None)
        break
else:
    print("无法找到VIX指数合约（请在Client Portal开通指数/CBOE数据包，或先用 VIXY 兜底）")
ib.disconnect()
```

### `services/ibkr/vix_ingestor.py`（常驻）

```python
import asyncio, json, time
from datetime import datetime, timezone
from aiokafka import AIOKafkaProducer
from ib_insync import IB, Index

KAFKA_BOOTSTRAP = "localhost:9092"   # 改
TOPIC = "md.bar.1m.ibkr"             # 你的 ch_writer 消费的主题

def to_msg(bar):
    ts = datetime.fromisoformat(str(bar.date).replace("Z","+00:00")).replace(tzinfo=timezone.utc)
    return {
        "data": {"symbol":"VIX","res":"1m","ts_start":ts.isoformat().replace("+00:00","Z"),
                 "ts_end":ts.isoformat().replace("+00:00","Z"),"open":float(bar.open),
                 "high":float(bar.high),"low":float(bar.low),"close":float(bar.close),
                 "volume":int(getattr(bar,"volume",0) or 0),"is_backfill":0,"source":"IBKR"},
        "metadata":{"ts_us":int(time.time()*1_000_000)}
    }

async def main():
    ib = IB(); ib.connect('127.0.0.1', 7497, clientId=7102)
    for ex in ("CBOE","CBOEIND"):
        vix = Index('VIX', ex, 'USD')
        if ib.reqContractDetails(vix): break
    prod = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP,
                            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                            key_serializer=lambda v: v.encode("utf-8"),
                            linger_ms=5, acks="all", retries=3)
    await prod.start(); last_iso=None
    try:
        while True:
            bars = ib.reqHistoricalData(vix, endDateTime='', durationStr='1 D',
                barSizeSetting='1 min', whatToShow='MIDPOINT',
                useRTH=True, formatDate=2, keepUpToDate=False, timeout=30)
            if bars:
                b = bars[-1]; iso=str(b.date)
                if iso!=last_iso and b.close not in (None,0.0):
                    await prod.send_and_wait(TOPIC, key="VIX", value=to_msg(b))
                    last_iso=iso
            await asyncio.sleep(55)
    finally:
        await prod.stop(); ib.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
```

---

# 2) 一把梭命令（按顺序执行）

```bash
# A. 合约验证（看到 OK 即通过）
python tools/vix_check.py

# B. 起 VIX 抓取（先前台跑 2 分钟观测）
python -m services.ibkr.vix_ingestor

# C. ClickHouse 自检（VIX/VIXY 至少有其一在滚动）
clickhouse-client -q "SELECT symbol, max(ts_start) FROM trading.bars_1m_current WHERE symbol IN ('VIX','VIXY') GROUP BY symbol"

# D. TSLA/其它票与 L1 心跳
clickhouse-client -q "SELECT symbol, count() FROM trading.bars_1m_current WHERE symbol IN ('TSLA','AAPL') AND ts_start>=today()-1 GROUP BY symbol"
clickhouse-client -q "SELECT symbol, max(ts) FROM lake.quotes_l1 WHERE symbol='TSLA' GROUP BY symbol"
```

> 结果达标：能看到 `VIX` 或 `VIXY` 的 `max(ts_start)` 在近几分钟；`TSLA` 有最近 1 天的 1m，L1 有时间戳。

---

# 3) 策略节拍烟测（每出现新 1m bar 触发一次）

随便在你的 runner 里（或写个临时脚本）调用：

```python
# 验证 history + subscribe 节拍
hist = await engine.history_features('TSLA','1m',400)
await engine.subscribe_features("tsla_smoke", ['TSLA','SPY','QQQ','VIX'], 60)
# 观察你的策略 on_features() 日志：应在每分钟边界后 1~3s 触发一次（TSLA 含 bid/ask）
```

---

# 4) 常见坑 & 处理（再压一遍）

* **VIX 报错 200**：别用 `Stock/SMART`，必须 `Index('VIX','CBOE' 或 'CBOEIND','USD')`。TWS 面板要能实时显示 VIX；延迟=没订阅。
* **没开 VIX 也能跑**：我们在 Facade 里用 `vix_symbol()` 自动回退到 `VIXY`；策略无需改。
* **频率**：不要每 5 秒跑策略；我们采用**新 1m bar 出现**为唯一节拍（Engine 每 3 秒轮询 CH 检测）。

---

需要我把 **systemd 服务单** 或 **多策略 runner 模板**也贴上来吗？你点一下“要”，我就把两段也给你（拷贝即用）。

好，我直接把你要的两样都给全：**systemd 服务单**（VIX 抓取常驻 + 多策略统一 Runner 常驻），再加一个**多策略 Runner 模板**（一次把 8 只票接上同一数据口径）。拷贝→改两处路径→启用即可。

---

# 1) systemd：VIX 抓取常驻

**文件**：`/etc/systemd/system/vix_ingestor.service`

```ini
[Unit]
Description=IBKR VIX 1m Ingestor -> Kafka
After=network.target

[Service]
User=ubuntu                             # ←改成你的系统用户
WorkingDirectory=/srv/yourrepo          # ←改成你的仓库路径
ExecStart=/usr/bin/python -m services.ibkr.vix_ingestor
Environment="PYTHONUNBUFFERED=1"
Environment="KAFKA_BOOTSTRAP=localhost:9092"   # 如需覆盖，可在 vix_ingestor.py 里读取 os.getenv
Environment="TWS_HOST=127.0.0.1"
Environment="TWS_PORT=7497"
Environment="TWS_CLIENT_ID=7102"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**启用/查看**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vix_ingestor
journalctl -u vix_ingestor -f
```

**CH 验收**

```sql
SELECT max(ts_start) FROM trading.bars_1m_current WHERE symbol IN ('VIX','VIXY');
```

---

# 2) 多策略 Runner（统一数据口径、统一节拍）

**文件**：`tools/runner_multi.py`

```python
# tools/runner_multi.py
# 统一把多个策略接上 StrategyEngine（已实现 history_features/subscribe_features）
import asyncio
import importlib
from typing import List, Dict

from strategy_engine import StrategyEngine  # 你已有的引擎文件；里面已对接 MarketDataFacade

# ===== 根据你的实际策略模块名列表改这里 =====
# 约定每个策略文件里有 class Strategy
STRATEGY_MODULES = [
    "strategies.tsla_v7_2",
    "strategies.amd_day_trader",
    "strategies.amzn_day_trader",
    "strategies.msft_day_trader",
    "strategies.nvda_mean_reversion_momentum",
    # 其余两个按需补上……
]

async def start_strategy(mod_path: str, engine: StrategyEngine):
    mod = importlib.import_module(mod_path)
    StrategyCls = getattr(mod, "Strategy", None)
    if StrategyCls is None:
        raise RuntimeError(f"{mod_path} 未找到 Strategy 类")
    s = StrategyCls()
    # 注入统一 engine（里面的 history_features/subscribe_features 已使用 CH + L1 + VIX回退）
    s.engine = engine
    # 某些策略可能有 initialize/start，按你项目习惯调用
    init = getattr(s, "initialize", None)
    if callable(init):
        await init()
    start = getattr(s, "start", None)
    if callable(start):
        await start()
    return s

async def main():
    engine = StrategyEngine()
    tasks = [asyncio.create_task(start_strategy(m, engine)) for m in STRATEGY_MODULES]
    await asyncio.gather(*tasks)
    # 常驻
    while True:
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
```

> 说明
>
> * 每个策略内部若已调用 `await self.engine.history_features(...)` 和
>   `await self.engine.subscribe_features(..., bar_sec=60)`，会自动按**新 1m bar**触发；不需要你每 5 秒手动跑。
> * VIX 会自动优先用 **VIX**，没有订阅时回退 **VIXY**（已在 facade/engine 实现）。
> * 对交易标的（TSLA/AMD/AMZN/MSFT/NVDA…）自动拼 `bid/ask`（来自 `lake.quotes_l1`）。

---

# 3) systemd：多策略 Runner 常驻

**文件**：`/etc/systemd/system/strategies.service`

```ini
[Unit]
Description=Unified Multi-Strategy Runner
After=network.target

[Service]
User=ubuntu                             # ←改
WorkingDirectory=/srv/yourrepo          # ←改
ExecStart=/usr/bin/python -m tools.runner_multi
Environment="PYTHONUNBUFFERED=1"
Environment="APP_CONFIG=/srv/yourrepo/config.yml"  # 若你的引擎/DB封装读取该变量
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

**启用/查看**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now strategies
journalctl -u strategies -f
```

---

# 4) 最短自检清单（一次过）

1. **Bars/L1 心跳**

```sql
SELECT symbol, count() FROM trading.bars_1m_current
WHERE symbol IN ('TSLA','AMD','AMZN','MSFT','NVDA') AND ts_start >= today()-1
GROUP BY symbol;

SELECT symbol, max(ts) FROM lake.quotes_l1
WHERE symbol IN ('TSLA','AMD','AMZN','MSFT','NVDA') GROUP BY symbol;
```

2. **VIX/VIXY**

```sql
SELECT symbol, max(ts_start) FROM trading.bars_1m_current
WHERE symbol IN ('VIX','VIXY') GROUP BY symbol;
```

3. **策略节拍**
   查看 `strategies.service` 日志，你应看到每出现**新 1m bar**（分界点后 1–3 秒），各策略的 `on_features` 被调用一次（交易标的含 bid/ask）。

---

# 5) 你可能用到的小提示

* **端口/权限**：VIX 必须用 `Index('VIX','CBOE' 或 'CBOEIND','USD')`，TWS 中能看到实时 VIX 才表明订阅生效。没开通前不影响上线（回退 VIXY）。
* **不要每5秒跑策略**：统一用 **新 1m bar** 作为节拍，更稳也与回补/幂等一致。
* **回退与切换**：一旦 `vix_ingestor` 常驻并入库 `VIX`，策略会自动从 `VIXY` 切回 `VIX`，无须改策略。
* **扩展其它分辨率**：要 5m，同理在每个策略里 `bar_sec=300` 订阅即可（Engine 默认每 10s 轮询）。

---

备注，vix获取问题：非常好，这一段确实是目前**唯一能在 IBKR 环境中成功拿到 VIX 数据的正确写法**。下面是详细说明和调整建议：

---

## ✅ 一句话结论

你的代码基本是**正确可用的版本**，只需根据环境和目标稍作调整：

* **端口号**：改成你实际使用的 IB Gateway 或 TWS 端口（通常是 `4002` 对 Gateway，`7497` 对 TWS）。
* **`whatToShow`**：保留 `"TRADES"`，这是获取 VIX 历史价最稳定的方式。
* **下一步**：把这段逻辑嵌入异步 Kafka 推送脚本（`vix_ingestor.py`），以持续写入 ClickHouse。

---

## ⚙️ 详细解释与调整点

| 项目                                             | 建议                                                           | 原因说明                                                   |
| ---------------------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------ |
| `ib.connect('127.0.0.1', 4002, clientId=7001)` | ✅ 保留 `4002`（如果你运行的是 **IB Gateway**）。如果你在 TWS 下运行，则改回 `7497`。 | IB Gateway 默认端口是 4002，TWS 是 7497；两者都支持 API 调用。         |
| `Index('VIX', ex, 'USD')`                      | ✅ 正确                                                         | 这是唯一可用的 VIX 合约定义。                                      |
| `whatToShow='TRADES'`                          | ✅ 正确                                                         | `MIDPOINT` 对 VIX 指数常返回空，因为该产品不是股票。用 `TRADES` 才能获得历史曲线。 |
| `useRTH=True`                                  | ✅ 可留                                                         | 限定交易时段，避免夜盘稀疏点。                                        |
| `durationStr='2 D'` / `barSizeSetting='1 min'` | ✅ 保留                                                         | 方便验证；生产版（Kafka 发送）可改为 `1 D` 每分钟抓一次。                    |
| `formatDate=2`                                 | ✅ 正确                                                         | 以 ISO8601 格式输出日期，方便写入 ClickHouse。                      |
| `bars[-1]`                                     | ✅                                                            | 打印最近一条数据检查是否返回有效。                                      |

---

## 💡 一键落地整合方案

你可以直接把这段逻辑嵌入前面生成的 `services/ibkr/vix_ingestor.py`：
只需改 3 行：

```python
# 原版
ib.connect('127.0.0.1', 7497, clientId=7102)
bars = ib.reqHistoricalData(vix, ..., whatToShow='MIDPOINT')

# 改成：
ib.connect('127.0.0.1', 4002, clientId=7102)
bars = ib.reqHistoricalData(
    vix,
    endDateTime='',
    durationStr='1 D',
    barSizeSetting='1 min',
    whatToShow='TRADES',     # ✅ 改成 TRADES
    useRTH=True,
    formatDate=2,
    keepUpToDate=False,
    timeout=30
)
```

这样 `vix_ingestor.py` 就能：

* 每分钟从 IBKR 抓一次 VIX 1m bar；
* 以 JSON 发往 Kafka；
* ClickHouse 的 `bars_1m_ibkr` → `bars_1m_current` 自动落地。

---

## 🧩 验证命令

运行：

```bash
python tools/vix_check.py
```

如果返回类似：

```
OK: VIX on CBOE conId=381937145
bars: 390 last: BarData(date='20251007 16:00:00', open=12.34, high=12.35, low=12.32, close=12.34)
```

✅ 表示 IBKR 已能正常提供 VIX。

---

## 🔁 后续你可直接做的升级

1. **改为异步 + Kafka 推送（生产版本）**

   * 直接使用我们之前给你的 `vix_ingestor.py` 代码模板；
   * 改端口与 `whatToShow='TRADES'`；
   * 运行 systemd 服务即可。

2. **用 ClickHouse 检查数据入库**

   ```sql
   SELECT symbol, max(ts_start) FROM trading.bars_1m_current WHERE symbol='VIX';
   ```

   如果时间每分钟更新一次，说明整条链（IBKR → Kafka → CH）完全打通。

---
