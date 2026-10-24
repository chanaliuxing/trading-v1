# SFS (滑动特征服务) 详细设计与集成立案 (v2.2 - 1s Redis 环缓存)

**文档版本**: 2.2
**核心架构决策（新增）**: 支持 1s 粒度，仅使用 Redis 环缓存（TTL=30–60 分钟），默认不落 ClickHouse，避免存储爆炸；用于超低延迟或线上对比实验，“需要的时候直接拿”。

说明：此前 v2.1 版本提出在 ClickHouse 中创建 `trading.sfs_features`。v2.2 引入“Redis-only 1s”路径作为默认选项。若未来需要持久化，则可按 v2.1 的 DDL 启用 CH 落地（单独开关）。

---

## 1. 目标与设计原则 (v2.0)
(保持不变)

---

## 2. 架构总览：数据融合架构

### 2.1. 数据流路径 (v2.2)
1.  **SFS-1s 路径 (滑动特征，Redis-only 默认)**:
    `Kafka(md.tick.trade.v1) -> SFS-1s服务 -> Redis(环缓存, TTL=30–60min)`
2.  **FeatureCalculator路径 (翻滚特征)**:
    `ClickHouse(trading.bars_1m) -> FeatureCalculator -> ClickHouse(写入 trading.features_1m 表)`
3.  **策略层 (数据融合)**:
    `StrategyEngine -> Strategy -> 读取 Redis(1s) + trading.features_1m`

---

## 3. 核心组件设计：SFS (v2.2)

### 3.1. 职责与边界
(保持不变，SFS仍只负责滑动特征)

### 3.2. 数据输出（Redis 环缓存）

- Key：`sfs:1s:<symbol>`
- 类型：LIST（LPUSH 最新在前）
- TTL：默认 3600 秒（可配 1800–3600）
- 长度：`max_len ≈ ttl_sec`，新增时 `LTRIM 0 max_len-1`
- 值：紧凑 JSON
  ```json
  {"ts": 1739971203000, "open": 175.12, "high": 175.20, "low": 175.11, "close": 175.18, "volume": 2200, "trades": 5}
  ```

实现：`services/market/sfs_1s_service.py`（aiokafka + redis.asyncio）。

（可选）ClickHouse 落地：保留 v2.1 DDL 作为备选方案，需要时再启用。

---

## 4. 现有系统集成方案 (v2.1)

### 4.1. 策略层改造：实现数据融合（Redis 1s + CH 1m）
策略在 `on_features()`（1m/5m）里补读 1s 子 bar 序列用于确认/微结构判断。

示例：
```python
from libs.dal.sfs_facade import SFS1sReader

class MyStrategy(StrategyAlgorithm):
    async def initialize(self):
        await self.ctx.subscribe_features(["AAPL"], bar_sec=60)
        self.sfs = SFS1sReader()

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        # 1) features 为 1m 翻滚特征
        # 2) 读取最近 30 条 1s 子bar（最新→更旧）
        subbars = await self.sfs.last_n(symbol, 30)
        # 3) 结合 1m + 1s 判断后做决策
        # ...
```
(其余部分保持不变)

---

## 5. 实施与上线路径 (v2.1)

实施计划中的数据库相关步骤已更新。

1.  **Phase 1: SFS-1s (Redis-only) 交付 (3–4 天)**
    *   [x] 服务落地：`services/market/sfs_1s_service.py`（本次提交已提供）
    *   [ ] 配置：环境变量 TTL/组名/flush 延迟（默认 TTL=3600, flush=250ms）
    *   [ ] 验证：Kafka→Redis 写入、延迟、内存占用、按 symbol 可用性
    *   [ ] 策略 PoC：`libs/dal/sfs_facade.py` 读取并融合 1m 特征

2.  **Phase 2（可选）：ClickHouse 持久化通道**
    *   [ ] 按 v2.1 DDL 启用 `trading.sfs_features`，仅在需要长期存储/回放时开启
    *   [ ] Writer 单独开关，避免默认写入

---

这份 **v2.2** 方案按“Redis-only 1s 环缓存”为默认，避免存储爆炸；当需要持久化或离线分析时，可切换/并行启用 v2.1 的 ClickHouse 通道，实现可观测与成本的平衡。
