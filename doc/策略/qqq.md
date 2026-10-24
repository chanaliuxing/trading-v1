QQQ 策略（当前实盘实现口径）

本文档描述 `qqq_intraday_001` 在当前代码与配置下的真实行为。
对应实现：`strategies/qqq_intraday_strategy.py`
对应配置：`config.yml -> qqq_intraday_001`

## 1. 日内时间框架（ET）

1. 00:00:00：跨日重置状态（交易次数、持仓态、锁参态等）。
2. 09:30:00-09:30:10：构建 `Price_base`（开盘 10 秒加权价）。
3. 09:30:00-09:45:00：累计 VWAP、统计 CrossCount、准备 09:45 锁参。
4. 入场评估窗口：默认 `09:30:00-14:00:00`（`entry_model=v47_fast_drift`）。
5. 收盘前强平：`15:59:00`（取消/平仓流程）。

## 2. 必需数据

策略依赖以下实时数据：

1. QQQ 1s/1m 聚合（close、volume、VWAP 基础量）。
2. TQQQ / SQQQ 1s 与 5m（用于执行与 ATR）。
3. ATR(14)（执行标的 5m）。

说明：旧版依赖的 `AD-NASD` / `ADD` 速度门已退役，不再作为入场门。

## 3. 09:30 开盘锚定与 09:45 锁参

1. `Price_base` 优先使用 09:30:00-09:30:10 的 1s 加权价。
2. 若窗口数据缺失，按优先级恢复：
   1. SFS 回补 09:30 窗口。
   2. 启动时 10 秒锚点。
   3. IBKR 历史 1s / 1m 兜底。
3. 09:45 后会锁 `amp` 档位并记录 `add/dev/target` 参数。
4. `toxic_day`（CrossCount>=3）会被记录，但当前实现不作为硬拦截门。

## 4. 入场评估（每秒评估，不是每分钟一次）

### 4.1 先过系统门（任何一条失败都不下单）

1. 交易日与交易时段门。
2. 当日交易次数门：
   1. `success_count >= max_trades_per_day` 直接拒绝。
   2. 第 3 笔前要求前两笔后当日已实现 PnL > 0。
3. 内存锁门：有 pending decision 时拒绝重复入场。
4. 开单冲突门：存在未终态 open orders 时拒绝。
5. 实盘持仓门：检测到账户已有 TQQQ/SQQQ 仓位则拒绝新开。
6. 数据质量门：QQQ 新鲜度、ATR 可用性、分钟量能等必须达标。

### 4.2 方向判定

1. `QQQ > VWAP`：仅考虑 LONG（执行 TQQQ）。
2. `QQQ < VWAP`：仅考虑 SHORT（执行 SQQQ）。
3. `QQQ == VWAP`：拒绝（direction_mismatch）。

### 4.3 v47_fast_drift 主门

1. `atr_rank` 过滤：当 `atr_rank <= atr_rank_skip_max`（默认 0.15）时拒绝。
2. 09:39-09:45 `early_override`：满足早盘结构与量能条件可提前放行。
3. 09:45-10:00 `delayed_confirm`：要求 5 分钟位移/ATR_D 通过确认。
4. 动能符号门：`v3m_signed = QQQ_now - QQQ_180s_ago` 与方向一致。
5. 两种可放行路径：
   1. `FAST`（当前默认 `enable_fast=false`，通常关闭）。
   2. `DRIFT`（3 根 1m 连续漂移 + 累计漂移阈值）。

### 4.4 AB 双阶段（`elite_ab_dual_mode=true`）

1. 开盘阶段（约 09:30-09:40）优先看 `OPEN_BREAKOUT`。
2. 常规阶段走 `A` 状态机（WAIT_MARK -> WAIT_PULLBACK -> ARMED）。
3. 通过后形成 `ELITE_AB_OPEN_BREAKOUT` 或 `ELITE_AB_A` 放行路径。

## 5. 下单执行

1. 先算总仓位（按 notional / price）。
2. 分两段：
   1. Probe（默认 70%）先下市价。
   2. Add（剩余 30%）在入场后最多 2 分钟内触发。
3. Add 触发阈值：价格相对入场价再走 `0.08 * entry_atr_used`。

## 6. 持仓风控与退出

### 6.1 初始防守

1. 初始止损距离：`max(2.5 * ATR, 0.50)`。
2. 止损价按执行标的计算并下发保护 stop intent。
3. 审计线程持续检查保护单是否存在，缺失会自愈补发；连续缺失触发 kill flatten。

### 6.2 退出机制

1. Hard stop：持仓满 `hard_stop_delay_min`（默认 3 分钟）后，若价位跌破 hard-stop 线则平仓。
2. TP1：触及 `target_px` 后先平约 50%。
3. 剩余仓位 trailing：按 `max(floor_line, trail_line)` 触发离场。
4. Logic retreat：最短持有时间后，每分钟检查 QQQ 收盘相对 VWAP，错误侧则平仓。
5. EOD 强平：15:59 无条件平仓流程。

## 7. 当日行为约束

1. 当前不是“每天只做一笔”；默认最多 3 笔（受 `max_trades_per_day` 控制）。
2. `is_traded` 是单笔持仓态，不等于当日永久锁死。
3. 真正的日级限制由 `success_count` 与日内 PnL 门共同约束。

## 8. 与旧文档差异（重要）

1. 已移除 ADD / AD-NASD 入场门。
2. 入场不是“每分钟 :00 检查”，而是每秒评估。
3. 入场主窗不是固定 09:45-15:45，而是随模型配置（当前 09:30-14:00）。
4. 止盈不是“触 TARGET 全平”，而是 TP1 + trailing 组合。
5. 强平时间是 15:59，不是 15:45。
