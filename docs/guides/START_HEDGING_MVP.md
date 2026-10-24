# HedgingService MVP - 快速启动指南

## ✅ 已完成

1. ✅ **创建 HedgingService 骨架** (`services/risk/hedging_service.py`)
   - 最小可运行版本 (MVP)
   - 核心逻辑：exposure_ratio × market_multiplier
   - 支持 VIX 分位数、ATR 趋势、Carry 信号
   - EMA 平滑 + 滞后带防抖

2. ✅ **配置文件更新** (`scripts/ai_trader/config/ai_trader.yaml`)
   - 添加 `risk_management.hedge` 配置块
   - 默认 `enabled: false` 和 `dry_run: true`

3. ✅ **集成到 run_system.py**
   - 在 RuntimeReconciler 后启动
   - 在 stop() 中正确关闭

4. ✅ **数据验证脚本** (`scripts/verify_hedging_data.py`)
   - 验证 VIX/SPY 历史数据
   - 检查 signals_realtime 表

---

## 📊 当前数据状态

```bash
python scripts/verify_hedging_data.py
```

**结果：**
- ❌ VIX: 0 rows (需要补充数据) → **暂时用 VIX=0.5 fallback，不影响骨架测试**
- ✅ SPY: 485 rows, 717 days → 足够计算 ATR 趋势
- ✅ Positions: signals_realtime 表不存在 → 正常，等策略运行后自动生成

---

## 🚀 启动步骤

### Phase 1: DRY RUN 测试（只记录日志，不执行）

#### 1. 启用 HedgingService（dry run 模式）

编辑 `scripts/ai_trader/config/ai_trader.yaml`:

```yaml
risk_management:
  hedge:
    enabled: true   # 从 false 改为 true
    dry_run: true   # 保持 true（DRY RUN）
```

#### 2. 启动系统

```bash
# 确保基础设施在运行
make up  # 或 cd infra && docker-compose up -d

# 启动交易系统（paper mode）
python run_system.py
```

#### 3. 观察日志

在日志中搜索：

```bash
tail -f logs/trading_system.log | grep -i hedge
```

**期望看到：**

```
[info] HedgingService initialized | enabled=True dry_run=True check_interval=60 ...
[info] 🛡️  HedgingService STARTED (dry_run=True)
[info] 🛡️  HEDGE DECISION | net_delta=... exposure_ratio=... target_ratio_ema=... dry_run=True
[info]    [DRY RUN] Would execute hedge adjustment | delta_gap=...
```

**如果看到：**
```
[info] HedgingService disabled in config
```
说明 `enabled: false`，回到步骤 1。

---

## 📝 预期行为（DRY RUN 模式）

### 正常情况（有仓位时）

每 60 秒输出一条 `HEDGE DECISION` 日志：

```
[info] 🛡️  HEDGE DECISION
  net_delta=35000           # 当前净 Delta 敞口
  gross_delta=50000         # 总敞口
  exposure_ratio=0.70       # 敞口比例 (35k / 50k max)
  trend_score=1.23          # SPY 趋势分数（正数=上涨）
  vix_pct=0.45              # VIX 分位数 (0~1)
  carry=-0.12               # 波动率 Carry
  target_ratio_raw=0.55     # 原始对冲比例
  target_ratio_ema=0.52     # EMA 平滑后
  target_hedge_delta=-18200 # 目标对冲 Delta
  current_hedge_delta=-10000 # 当前对冲仓位
  delta_gap=-8200           # 需要补的缺口
  dry_run=True
[info]    [DRY RUN] Would execute hedge adjustment | delta_gap=-8200
```

### 无仓位时

```
[debug] No exposure, skipping hedge cycle
```

### 数据问题（VIX 缺失）

```
[warning] Insufficient VIX data | rows=0
```

**临时解决方案：** VIX 缺失时会 fallback 到 `vix_pct=0.5` (中位数)，不影响骨架测试。

---

## 🛠️ 下一步工作

### Phase 2: 补充 VIX 数据

```bash
# TODO: 从 IBKR 或其他数据源回填 VIX 历史数据
# 目标: 至少 252 天数据 (1 年交易日)

# 临时方案: 如果 MarketDataService 支持，添加 VIX 到 backfill 列表
```

### Phase 3: 实现真实执行

修改 `services/risk/hedging_service.py`:

```python
# 当前代码 (Line ~175):
if self.dry_run:
    logger.info("   [DRY RUN] Would execute hedge adjustment", delta_gap=delta_gap)
else:
    # TODO: Implement real execution via OptionsSelector
    logger.warning("   Real execution not yet implemented, skipping")

# 需要改为:
if self.dry_run:
    logger.info("   [DRY RUN] Would execute hedge adjustment", delta_gap=delta_gap)
else:
    await self._execute_adjustment(delta_gap, carry, theta_budget)
```

然后实现 `_execute_adjustment()` 中的：
- `_add_option_hedge()` → 调用 `OptionsSelector.select_hedge_contract()`
- `_reduce_option_hedge()` → 查询现有对冲仓位并卖出

### Phase 4: 参数校准

运行 paper trading 2 周后，基于真实数据校准：

```python
# 在 ai_trader.yaml 中调整这些参数:
dynamic_params:
  smooth_alpha: 0.3        # 如果频繁调仓 → 降低到 0.2
  hysteresis: 0.05         # 如果仍然频繁 → 提高到 0.08
  theta_budget_pct: 0.0005 # 基于实际 PnL 波动调整
  max_net_delta_usd: 50000 # 基于最大历史敞口 × 0.8
```

---

## 🧪 单元测试（TODO）

```bash
# 未来添加
pytest tests/unit/services/risk/test_hedging_service.py -v
```

**测试覆盖：**
- [ ] `_calc_target_ratio()` 边界条件
- [ ] `_get_spy_trend_score_atr()` Wilder ATR 正确性
- [ ] `_get_vix_and_carry_factors()` 分位数计算
- [ ] EMA 平滑逻辑
- [ ] 滞后带触发条件

---

## 📚 相关文档

- **设计文档**: `./doc/hedge/1.md` (外部AI建议的完整版)
- **CLAUDE.md**: 生产就绪清单 (Section VIII-IX)
- **OptionsSelector**: `scripts/ai_trader/risk/options_selector.py` (已存在)
- **配置参考**: `scripts/ai_trader/config/ai_trader.yaml:207-240`

---

## ❓ 常见问题

### Q: VIX 数据缺失会影响吗？

**A:** 短期影响不大。VIX 缺失时会 fallback 到 `vix_pct=0.5` (中性值)，此时对冲比例主要由 `exposure_ratio` 和 `trend_score` 决定。但长期建议补充数据。

### Q: 为什么没看到 HEDGE DECISION 日志？

**A:** 可能原因：
1. `enabled: false` → 检查 `ai_trader.yaml:213`
2. 没有仓位 → 等策略开始交易后会产生敞口
3. 在冷却期内 (cooldown_sec=300) → 等 5 分钟

### Q: signals_realtime 表不存在怎么办？

**A:** 正常。这个表由 StrategyEngine 运行时动态创建，在策略开始交易后会自动出现。

### Q: 如何验证 ATR 计算正确？

**A:** 对比手动计算：
```python
# 在 Python REPL 中
from services.risk.hedging_service import HedgingService
import asyncio

hs = HedgingService()
score = asyncio.run(hs._get_spy_trend_score_atr())
print(f"SPY Trend Score: {score}")  # 应该在 [-3, 3] 范围
```

---

## ✅ 验收标准

**Phase 1 完成标准：**
- [ ] `python run_system.py` 启动无报错
- [ ] 日志显示 "HedgingService STARTED (dry_run=True)"
- [ ] 有仓位时每 60s 看到 `HEDGE DECISION` 日志
- [ ] `delta_gap` 计算合理（与实际仓位相关）
- [ ] Ctrl+C 关闭时干净退出，无异常

**Phase 2/3 验收标准：** 见下一轮迭代

---

## 📞 联系与支持

如果遇到问题：
1. 检查日志：`logs/trading_system.log`
2. 验证配置：`python scripts/verify_hedging_data.py`
3. 测试单个函数：用 Python REPL 直接调用 `HedgingService` 方法

**Last Updated:** 2025-11-20
