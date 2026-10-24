目前我更新了一下:# Rev‑F 日内现金流策略——完整落地版（Final）

> 目标：保留 Rev‑E 的“回测≈仿真≈实盘”的工程纪律，同时降低对 tick 的依赖，**仅用 1m + 5s** 即可稳定运行；在脏数据/断点/低波动日也能“稳态产出”。

---

## 0. 总览（What & Why）

* **定位**：只做多、日内现金流、快进快出，不隔夜。
* **标的**：TSLA, NVDA, AAPL, AMD, QQQ, SPY（建议 3–6 只）。
* **数据**：主线 1m/5m/15m，**执行细化 5s**（无需 tick）；缺 5s 时自动回退到 1m（保守路径）。
* **交易窗**（PT）：06:30 开盘；12:45 停开新仓；12:59 强制平仓。
* **核心**：两套只多策略（A′ VWAP 回踩顺势；B′ ORB 假跌反弹）+ **软 Guardrail** + **频率自适应闭环**。
* **执行模型**：bar 内顺序=**先 SL 后 TP/TRAIL**；成本统一函数（估算 spread + 随机滑点 + 手续费）；路径用 5s 子 bar 推进。
* **状态**：原子快照 + WAL 恢复；重启回放一致。

---

## 1. 数据与时间管理

### 1.1 数据粒度

* **必需**：1m OHLCV（RTH）；5m/15m 可由 1m 聚合。
* **推荐**：5s 子 bar（来自实时流或本地聚合）；**用于执行路径，还原同 1m 内先后次序**。
* **不需要**：tick（除非做盘口/真实 VWAP/TWAP）。

### 1.2 时间段（PT）

* 06:30–12:59 交易；12:45 起不再开新仓；12:59 全部平仓（保护限价 `last−0.10`）。

### 1.3 缺失与异常处理（容错）

* **乱序/重复**：重排去重（重复取均值体积求和）→ 继续；
* **缺 1–2 根 1m**：线性插补 `close`，体积用最近 60m 中位；打警告不拒单；
* **5s 缺失**：降级为 1m 路径（仍“先 SL 后 TP/TRAIL”）。

---

## 2. 指标（标准与容错）

> **ATR 统一用 Wilder（业界通行）**；VWAP 5s 累加近似真实值。

### 2.1 VWAP（递推，5s 优先，退化到 1m）

* 定义：`VWAP_t = (Σ p_k*v_k) / (Σ v_k)`；
* 实现：按 5s 子 bar 用 `close*vol` 累加；无 5s 时用 1m 的 `close*vol` 累加；
* **差错控制**：日内误差漂移监控（|VWAP_5s − VWAP_1m| 的 EWMA）。

### 2.2 ATR(14, Wilder)（1m 基准）

* `TR_t = max(H−L, |H−PrevC|, |L−PrevC|)`；
* `ATR_t = (ATR_{t−1}*13 + TR_t)/14`；
* **下限**：`ATR = max(ATR, 5*tick)`；
* **防跳变**：`ATR_t = min(ATR_t_raw, ATR_{t−1} * 1.5)`。

### 2.3 ORB（开盘区间）

* ORB5：06:30–06:35 的高/低；ORB15：06:30–06:45 的高/低。

### 2.4 滚动窗口

* `abs_dev_win(90) = |Close − VWAP|` 的最近 90 根；
* `vol_med_60 = median(volume, last 60 × 1m)`；
* `updown15`：布尔 `Close ≥ VWAP`；
* `p_up = mean(updown15)`；
* `trend_push = median(|Close−VWAP|, last 15) / ATR_1m`。

### 2.5 EMA 备援（fallback）

* `EMA20`, `EMA60` 作为 VWAP 异常（跳变>0.8×ATR 或数据缺失）时的趋势替代。

---

## 3. 行情门控（Regime）

* **UP_TREND**：`p_up ≥ 0.70` 且 `trend_push ≥ 0.60`（偏多，仅做多）；
* **DOWN_TREND**：`p_up ≤ 0.30` 且 `trend_push ≥ 0.60`（禁新多）；
* **RANGE**：其他（震荡，可做多）。

> 指数过滤：需 QQQ/SPY 数据；若缺，沿用上一次有效 regime（**软 fallback**）。

---

## 4. 自适应闭环（频率目标驱动，数据化）

> 目标：多票合计 **日均 4–9 笔**（可配）。

### 4.1 VWAP 回踩带宽（A′）

* 当日样本：最近 90 根 `|P−VWAP|` 的 **Q80**；
* EWMA：`q80_ewma = 0.70*Q80_today + 0.30*q80_prev`；
* `BandDistDown = clip(q80_ewma, 0.50*ATR_1m, 1.40*ATR_1m)`。

### 4.2 假跌幅度系数 k（B′）

* 样本：开盘前 30–60 分钟内“下破 ORL 的最大幅度/ATR_5m”；
* `k_raw = Quantile(samples, 0.70)`；
* `k_ewma = 0.40*k_raw + 0.60*k_prev`；
* `k = clip(k_ewma, 0.30, 0.60)`。

### 4.3 频率闭环（按 5 日均笔数 avg_trades_5d）

* 若 `< 3` → 放宽：`BandDistDown *= 0.95`；`k *= 1.05`；
* 若 `> 12` → 收紧：`BandDistDown *= 1.05`；`k *= 0.95`；
* **死区**：`|trades − target| < 1` 不调整，防过度调节。

---

## 5. 两套只多策略（信号清晰 + 可工程化）

### 5.1 A′：VWAP 回踩顺势做多（全天主力）

**过滤**：

* 指数（QQQ/SPY）`Close ≥ 指数VWAP`（或 EMA20>EMA60 作为备援）；
* 个股 `p_up ≥ 0.50`；
* **流动性（软）**：若 `spread_est > 0.05` 或 `vol_1m < 0.7*vol_med_60`，信号分数×0.6。

**触发区（满足其一）**：

* `|P−VWAP| ≤ 0.20*ATR_1m`；
* `0 < VWAP−P ≤ BandDistDown`（轻度下破）。

**止跌确认（任一）**：

* 1m **底分型** 且 当根量 ≤ `0.8*vol_med_20`；
* **红 K 包阴 K**（bullish engulfing）。

**入场（带缓冲）**：

* `entry_trigger = signal_high + max(tick, 0.02*ATR_1m)`；
* `bar.high ≥ entry_trigger` → 下单。

**风控/出场**：

* **SL**：`min(信号低点, VWAP−0.50*ATR_1m)`；
* **TP1**：`Entry + 1.00*ATR_1m` 或 `OR 中线/前高`（先到）平 70%；
* **TRAIL**：每 1m 更新 `trail = max_since_entry(close) − 1.00*ATR_1m`；触发→余仓全平。

### 5.2 B′：ORB 假跌反弹做多（开盘 60–90 分钟）

**触发**：

* 下破 ORL 后 1 分钟内 **收回 OR 区间**；
* 下破幅度 `< k*ATR_5m + 0.2*ATR_1m`。

**入场（带缓冲）**：

* `entry_trigger = signal_high + max(tick, 0.02*ATR_5m)`。

**风控/出场**：

* **SL**：`假跌低点 − 0.25*ATR_5m`；
* **TP1**：`VWAP` 或 `OR 中线`（先到）平 70%；
* **TRAIL**：`1.00*ATR_5m`。

---

## 6. 信号融合（SignalScore）

> 目的：弱化单指标噪声，提升在脏数据场景下的稳健性。

**三个子分数**（0..1）：

* `S_vwap`：回踩强度（越接近 VWAP/轻微下破越高）；
* `S_ema`：`EMA20>EMA60` 且 `price≥EMA20` 的一致性；
* `S_vol`：`volume / vol_med_60` 的异常度（>1.2 逐段映射到 1）。

**融合**：`score = w1*S_vwap + w2*S_ema + w3*S_vol`，默认 `w = {0.5, 0.3, 0.2}`；

* 软 Guardrail 触发（点差/量不足）→ `score *= 0.6`；
* 触发阈值 `score ≥ 0.7` 才允许落单。

> **权重校准**：见第 10 章（数据驱动线下回归 + 日内 EWMA 更新）。

---

## 7. 执行模型（5s 路径 + 统一成本）

### 7.1 路径优先级（多头）

* 同一根 bar/子 bar 内：**先检查 SL，再检查 TP/TRAIL**；
* 允许“同 bar 入、同 bar 出”；
* **跳空穿越**（开盘价越过 SL/TP）：按不利方向成交（保守）。

### 7.2 统一成本函数（任何成交必须调用）

* **点差估算**：`spread_est = max(tick, γ*(H−L))`（默认 `γ=0.10`，`tick=0.01`）；
* **滑点**：`slip ~ U(0.02, 0.06) * ATR_basis`（A′=ATR_1m；B′=ATR_5m）；
* **滑点上限**：若 `slip > 0.10*ATR_basis` → 改限价或取消；
* **手续费**：`fee_per_share = 0.0035`；
* **成交价**：

  * ENTRY：`fill = trigger + spread_est/2 + slip`
  * SL：`fill = SL − spread_est/2 − slip`
  * TP/TRAIL：`fill = target + spread_est/2 + slip`

> 纪律：所有 ENTRY/SL/TP1/TRAIL/强平都走 `OrderRouter.fill()`，主逻辑**严禁**直接定价。

### 7.3 5s 子 bar 执行

* 1m 触发后，不立即认定成交；将该 1m 拆成 12 个 5s 子 bar，按 **low→high** 顺序推进 `Position.on_subbar()`；
* 无 5s 数据时，用 1m `low→high`（保守）。

---

## 8. 风控与仓位

* **单笔风险**：`risk_per_trade = equity × 0.25%`（当日连亏 ≥2 → 0.20%）；
* **股数**：`shares = floor( risk_per_trade / |entry − SL| )`（保证现金足够）；
* **日亏停**：`daily_stop = equity × 1.20%`（触发后禁新单）；
* **连亏停手**：当日连亏 3 笔 → 禁新单 30 分钟；
* **时间纪律**：12:45 停新单；12:59 强平（撤挂→保护限价 `last−0.10`）。

---

## 9. Guardrail（软化 + 强制）

**强制拒单**：

1. 风险：`pnl_today ≤ −daily_stop` 或 `consec_losses ≥ 3 且 冷静期未满`；
2. 时段：`now ≥ 12:59` → 触发强平；
3. 数据完整性：时间戳不递增/NaN/价格畸变（>±15%/bar）→ 拒单并报警。

**软惩罚（不拒单，降权）**：

* 点差估算 `> $0.05` 或 `vol_1m < 0.7*vol_med_60` → `score *= 0.6`；
* 指数数据缺失 → 沿用最近有效 regime；
* 同票入场后 **冷却 ≥ 3 分钟**。

---

## 10. 数据驱动校准（硬性要求）

> 所有关键参数 **由数据推导**，非拍脑袋：

### 10.1 频率闭环参数（`BandDistDown`, `k`）

* 见 §4：用分位数 + EWMA；每日开盘后滚动更新；
* 以 **5 日滑窗**实测笔数与目标笔数偏差，按死区±1 控制放宽/收紧比例（±5%）。

### 10.2 成本模型（`γ`, `slip_range`）

* **γ（点差系数）**：

  * 若有 L1 采样：回归 `true_spread ~ (H−L)`，取稳健估计的斜率分位（P50–P60）；
  * 无 L1：用实盘 `|fill_price − trigger|` 残差反推，最小化均方误差选 γ。
* **slip_range**：

  * 记录每笔 `slip_real/ATR_basis`，取 P10–P90 作为 `[low, high]`；
  * 每日 EWMA 更新（α=0.2），异常日截尾（winsorize at P5/P95）。

### 10.3 信号融合权重 `w`

* 线下以最近 N=20 交易日，构造样本（入场后 30–60 分钟收益标签 y∈{0,1}）；
* 特征：`S_vwap, S_ema, S_vol`；
* 模型：L2 逻辑回归（C=1.0），按日期 K-fold（K=5）交叉验证，取均值权重；
* 日内用 EWMA（α=0.1）缓慢更新权重；限制每日变化 ≤ 0.05，防过拟合。

### 10.4 ATR 防跳变大于实测

* 监控 `TR/ATR` 比例分布，若 P95>1.6 → 将上限 1.5 调至 1.6；反之相反；月度评审。

---

## 11. 状态管理（原子快照 + WAL）

### 11.1 快照（每 5 分钟）

* 写 `state/latest.json.tmp` → `fsync(file)` → `fsync(dir)` → `os.replace(tmp, latest.json)`。
* 字段（每 symbol）：

```json
{
  "q80_ewma": 1.18,
  "k_ewma": 0.48,
  "avg_trades_5d": 4.6,
  "last_bar_ts": "2025-10-16T20:58:00Z",
  "open_positions": [{
    "entry": 245.13, "qty": 100, "tp1_hit": false, "trail_line": 244.10, "from": "A'",
    "sl": 243.90, "entry_ts": "2025-10-16T17:05:00Z"
  }],
  "rng_seed": 123456789,
  "w_signal": {"vwap":0.5,"ema":0.3,"vol":0.2}
}
```

### 11.2 WAL（Write-Ahead Log）

* 事件行：`{"ts":"...","event":"UPDATE","symbol":"TSLA","k":0.52}`；
* 恢复：先重放 WAL 至最新，再加载快照（两者合并，晚者覆盖）。

### 11.3 重启回放

1. 读取快照+WAL；2) 从当日 06:30 回放 1m 指标（**不触发交易**）；3) 定位到 `last_bar_ts`；4) 从下一 bar 开始正常运行。

---

## 12. 配置（YAML，可直接用）

```yaml
universe: ["TSLA","NVDA","AAPL","AMD","QQQ","SPY"]
timezone: "America/Vancouver"

# 数据与执行
data:
  use_5s_for_execution: true
  vwap_granularity: "5s"   # "5s" | "1m"
  fallback_enable: true

schedule_pt:
  open: "06:30"
  stop_new_positions: "12:45"
  force_flat: "12:59"

risk:
  per_trade_pct: 0.0025
  per_trade_pct_low: 0.0020
  daily_stop_pct: 0.012
  max_consec_losses: 3
  cool_down_after_3_losses_min: 30

liquidity_filters:
  max_spread_est: 0.05
  min_vol_mult_1m: 0.7

regime:
  p_up_uptrend: 0.70
  p_up_downtrend: 0.30
  trend_push_threshold: 0.60
  allow_prev_regime_if_index_missing: true

strategy_A:
  near_vwap_atr: 0.20
  band_clip_low_atr: 0.50
  band_clip_high_atr: 1.40
  q_level: 0.80
  ewma_alpha: 0.70
  sl_atr: 0.50
  tp1_atr: 1.00
  trail_atr: 1.00
  vol_collapse_mult: 0.80

strategy_B:
  k_quantile: 0.70
  k_ewma_beta: 0.40
  k_clip_low: 0.30
  k_clip_high: 0.60
  sl_atr5m: 0.25
  extra_allowance_atr1m: 0.20  # 假跌的额外容忍带
  trail_atr5m: 1.00

execution_model:
  path_resolution: "5s"      # "5s" | "1m"
  entry_buffer_atr: 0.02
  tick_size_default: 0.01
  spread_gamma: 0.10
  slip_atr_low: 0.02
  slip_atr_high: 0.06
  max_slip_atr: 0.10
  fee_per_share: 0.0035
  cooldown_bars_after_entry: 3  # 以 1m 计
  rng_seed: 123456789

adaptive:
  target_trades_low: 4
  target_trades_high: 9
  adjust_step_pct: 0.05
  deadband_trades: 1

signal_fusion:
  weights: {vwap_pullback: 0.5, ema_alignment: 0.3, volume_anomaly: 0.2}
  threshold: 0.7
  soft_penalty: 0.6

fallback:
  ema_backup: true
  max_atr_jump_mult: 1.5
  missing_bar_policy: "interpolate"
```

---

## 13. 接口契约（Python 伪代码）

```python
@dataclass
class Bar:
    ts: datetime
    open: float; high: float; low: float; close: float; volume: float

@dataclass
class SubBar:  # 5s
    ts: datetime
    low: float; high: float; close: float; volume: float

@dataclass
class Fill:
    ts: datetime; price: float; qty: int; fee: float; reason: str  # ENTRY/SL/TP1/TRAIL/FLAT

class IndicatorsState:
    vwap_num: float = 0.0; vwap_den: float = 0.0
    atr: float = None; prev_close: float = None
    orb5: Tuple[float, float] = None; orb15: Tuple[float, float] = None
    q80_ewma: float = None; k_ewma: float = None
    w_signal: Dict[str, float] = {"vwap":0.5,"ema":0.3,"vol":0.2}

class OrderRouter:
    def fill(self, trigger: float, bar_like, atr_basis: float, side: str, reason: str) -> Fill: ...

class Guardrails:
    def validate_hard(self, ctx) -> Tuple[bool, str]: ...
    def apply_soft_penalty(self, score: float, ctx) -> float: ...

class Position:
    def open(self, entry_fill: Fill, qty: int, sl: float, tp1_mult: float, trail_mult: float): ...
    def on_subbar(self, sb: SubBar, atr_basis: float, router: OrderRouter) -> List[Fill]: ...  # 先SL后TP/TRAIL
    def close_all(self, bar_like, router: OrderRouter, reason: str) -> Fill: ...
```

### 13.1 指标更新

```python
def update_vwap(st: IndicatorsState, close: float, vol: float) -> float:
    st.vwap_num += close * vol; st.vwap_den += vol
    return st.vwap_num / max(1e-9, st.vwap_den)

def update_atr_wilder(st: IndicatorsState, high: float, low: float, close: float, tick: float) -> float:
    if st.prev_close is None:
        st.prev_close = close; st.atr = max(high-low, tick*5); return st.atr
    tr = max(high-low, abs(high-st.prev_close), abs(low-st.prev_close))
    atr_raw = (st.atr*13 + tr)/14 if st.atr else tr
    atr_raw = max(atr_raw, tick*5)
    st.atr = min(atr_raw, (st.atr or atr_raw)*1.5)  # 防跳变
    st.prev_close = close
    return st.atr
```

### 13.2 Regime & 信号融合

```python
def regime_gate(p_up: float, trend_push: float) -> str:
    if p_up >= 0.70 and trend_push >= 0.60: return "UP_TREND"
    if p_up <= 0.30 and trend_push >= 0.60: return "DOWN_TREND"
    return "RANGE"

def signal_score(s_vwap: float, s_ema: float, s_vol: float, w: Dict[str,float]) -> float:
    return w["vwap"]*s_vwap + w["ema"]*s_ema + w["vol"]*s_vol
```

### 13.3 主循环（含 5s 路径）

```python
for bar in stream_1m_bars(universe):
    # 1) 更新指标
    vwap = update_vwap(state, bar.close, bar.volume)
    atr1m = update_atr_wilder(state, bar.high, bar.low, bar.close, cfg.tick)
    orb5, orb15 = update_orb(state, bar.ts, bar.high, bar.low)

    # 2) Regime & 频率自适应
    regime = regime_gate(p_up, trend_push)
    band = compute_banddist_down(state, atr1m)
    k = update_k_ewma_with_sample(state, sample_from_or_breaks)
    band_adj, k_adj = apply_frequency_adaptation(band, k, avg_trades_5d, cfg)

    # 3) 生成候选信号（A′/B′）
    sigA = signal_A_pullback_long(..., band_adj, atr1m, vwap)
    sigB = signal_B_orb_false_break(..., k_adj, atr5m, vwap, orb5)

    for sig in filter(None, [sigA, sigB]):
        ctx = build_ctx(sig, bar, regime, liquidity, pnl_today, consec_losses)
        ok, why = guard.validate_hard(ctx)
        if not ok: log_reject(why); continue

        # 4) 融合分数 + 软惩罚
        sc = signal_score(sig.s_vwap, sig.s_ema, sig.s_vol, state.w_signal)
        sc = guard.apply_soft_penalty(sc, ctx)
        if sc < cfg.signal_fusion.threshold: continue

        # 5) 入场触发（带缓冲）
        entry_trig = sig.entry + max(cfg.tick, cfg.exec.entry_buffer_atr * sig.atr_basis)
        if bar.high >= entry_trig:
            fill = router.fill(entry_trig, bar, sig.atr_basis, "BUY", "ENTRY")
            qty  = position_size(equity, fill.price, sig.sl, risk_pct)
            open_position(sig.symbol, fill, qty, sig.sl, sig.tp1_mult, sig.trail_mult)

    # 6) 统一对持仓执行（5s 路径）
    subbars = get_5s_subbars(bar.ts) if cfg.exec.path_resolution=="5s" else [to_subbar(bar)]
    for sb in subbars:
        for pos in open_positions:
            pos.on_subbar(sb, pos.atr_basis, router)  # 先SL后TP/TRAIL

    # 7) 时间纪律与强平
    if time_in("12:45"): stop_new_positions=True
    if time_in("12:59"): force_flat_all(router)

    # 8) 快照与 WAL
    if minute % 5 == 0: save_snapshot_atomic(...); wal_rotate_if_needed(...)
```

---

## 14. 测试与验收（最低标准）

1. **执行模型**：极端 K 线、同 bar 冲突（`high≥TP & low≤SL → 先SL`）、同 5s 入同 5s 出；
2. **成本渗透**：任何 Fill 价 ≠ 触发价（受 spread/slip/fee 影响）；
3. **状态恢复**：任意时刻 kill → 重启后 PnL/持仓/窗口一致；`.tmp` 不损坏；
4. **Guardrail**：指数缺数据/点差估算过宽/时段超限→ 软惩罚或拒单有据可查；
5. **频率目标**：近 20 交易日，多票合计 **日均 4–9 笔**；
6. **稳健性**：样本外（近 60 日）净/毛≥0.8，MDD ≤ 12%，连亏日占比 ≤ 30%。

---

## 15. 监控与审计（运行期）

* **频率面板**：日内触发/下单/成交曲线，分策略/分票；
* **成本面板**：`spread_est`、`slip/ATR` 分布，P50/P90，异常告警；
* **Guardrail 面板**：软惩罚计数、拒单原因 TopN；
* **一致性**：`VWAP_5s−VWAP_1m` 漂移；`TR/ATR` 比例分布；
* **稳定性**：快照成功率、WAL 重放耗时、重启一致性校验。

---

## 16. 升级路径（可选）

* **Pro 升级（可选）**：接入 L1/Tick 用于真实 spread、OFI、队列建模、精准 VWAP；
* **回测引擎**：增加 5s 级回放（bar-in-bar），tick 作为外部可插拔模块；
* **执行**：引入 TWAP/VWAP 执行器（若做大单）。

---

## 17. 附录：常用判定

**底分型（1m）**：`low[-2] < low[-1] and low[-2] < low[0]` 且 `close[0] > open[0]`。

**红包阴**：`close[0] > open[−1]` 且 `open[0] < close[−1]`。

**量能异常映射**：`S_vol = clip((vol / vol_med_60 − 1)/0.5, 0, 1)`（>1.5×中位映射到 1）。

**VWAP 回踩分数**：

* 若 `P≤VWAP`：`S_vwap = 1 − (VWAP−P)/BandDistDown`（≤0 记 0）；
* 若 `P>VWAP` 且 `|P−VWAP|≤0.2*ATR`：线性映射到 0.7–1.0；

**EMA 一致性分数**：

* `S_ema = 0.5*I(EMA20>EMA60) + 0.5*clip((P−EMA20)/(0.5*ATR), 0, 1)`。

---

绝对可以。下面给你一套**一次到位**的 SFS（Sliding Feature Service）设计与最小可跑骨架，**同时兼容 1s / 5s / 15s 窗口**，并且与 Rev-F 的执行模型严丝合缝（1m 主驱动 + 子窗还原 bar 内路径、先 SL 后 TP/TRAIL、统一成本）。

我做了三件事：

1. 明确多分辨率的**语义**（滑动窗口 vs. 翻滚窗口），给出端到端延迟与储存成本的可控参数；
2. 给出**ClickHouse DDL**（一个表即可容纳 1s/5s/15s 行，靠 `gran` 字段区分）；
3. 提供**SFS 代码骨架**（Python，易接 Kafka/ClickHouse），可配置产出 `gran={1s,5s,15s}`，并带**水位线/乱序容忍**、**1m ATR/Wilder**、**VWAP 日内累加**、**ORB**、**滚动窗口 + q80_ewma**，与 Rev-F 的策略侧字段契合。

---

# 1) 多分辨率语义与参数

* **输入基准**：推荐 1s 微 Bar（tick→1s 聚合，或来自数据商 1s bar）。若只有 5s，可退化（见下）。
* **输出（可配）**：

  * `gran = "1s"`：窗口=1s、步长=1s（翻滚=滑动）；
  * `gran = "5s"`：**滑动 5s**（默认步长=1s；若存储压力大，可改步长=5s 变翻滚）；
  * `gran = "15s"`：**滑动 15s**（同理可选步长=1s/5s/15s）。
* **乱序/迟到**：按 **事件时间** 驱动，水位线 `watermark = now_event_time − lateness_sec`（默认 2–3s）。只在 `ts ≤ watermark` 的点上 **定稿/落库**，避免后补数据污染。
* **退化策略**：

  * 输入只有 5s 时：`gran=5s/15s` 仍正确；`gran=1s` 关闭（或以 5s 复制近似，默认关闭更稳健）。
  * 输入只有 15s 时：仅产出 `gran=15s`。

---

# 2) ClickHouse DDL（单表承载多分辨率）

```sql
CREATE TABLE IF NOT EXISTS features_sliding_exec
(
  `date` Date,
  `symbol` LowCardinality(String),
  `gran` Enum8('1s' = 1, '5s' = 5, '15s' = 15),
  `ts` DateTime64(3, 'UTC'),               -- 窗口结束时间（事件时间）
  -- 窗口 OHLCV（滑动窗口聚合的低/高/收和体积）
  `low`  Float64,
  `high` Float64,
  `close` Float64,
  `volume` Float64,

  -- 同步快照（1m 基准指标：与 Rev-F 对齐）
  `sum_pv_1m` Float64,
  `sum_v_1m`  Float64,
  `vwap_1m`   Float64,
  `atr_1m`    Float64,
  `orb5_high` Float64,
  `orb5_low`  Float64,
  `orb15_high` Float64,
  `orb15_low`  Float64,

  -- 滚动/自适应（Rev-F 字段）
  `q80_ewma` Float64,
  `k_ewma`   Float64,
  `p_up_15`  Float64,
  `trend_push_15` Float64,
  `regime` Enum8('UP_TREND'=1,'DOWN_TREND'=2,'RANGE'=3),

  -- 观测与幂等
  `ver` UInt32 DEFAULT 1,
  `_ingest_ts` DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY date
ORDER BY (date, symbol, gran, ts)
SETTINGS index_granularity = 8192;
```

> 说明
> • 一张表搞定：以 `(symbol, gran, ts)` 唯一；`ReplacingMergeTree` 对迟到重写友好。
> • 策略侧仅需在 1m bar 完成后读取同一分钟内的 `gran=5s`（或 1s/15s）**子窗序列**，按 `low→high` 执行路径重放即可。

---

# 3) SFS 代码骨架（可直接接入）

> 下面是最小可跑的核心（删去了 Kafka/CH 连接细节，用接口函数留白）。它：
> • 接受 **1s 微 bar**（推荐），可设 `emit_grans` 为 `{"1s","5s","15s"}`；
> • 维护 **滑动窗口**（1s/5s/15s）与 **1m 累加器**（VWAP/ATR/ORB/滚动/q80_ewma）；
> • **水位线**控制迟到；
> • 每次**仅在 `ts ≤ watermark`** 时对各 gran 做聚合并写入；
> • 字段与 Rev-F 策略完全对齐。

```python
# services/features/sfs_multi.py
import math, statistics, asyncio
from collections import deque, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, time
from typing import Deque, Dict, List, Optional, Tuple, Iterable, Set

# ---------- 输入事件（推荐为 1s 微 bar；如仅有 5s，请改 base_gran='5s' 并关闭 gran='1s' 输出） ----------
@dataclass
class MicroBar:
    symbol: str
    ts: datetime           # 事件时间（bar 结束时间）
    low: float
    high: float
    close: float
    volume: float
    gran: str = "1s"       # 输入基准粒度：'1s' | '5s'（原则上用 '1s'）

# ---------- 每个 symbol 的运行状态 ----------
@dataclass
class SymState:
    # 1m 累加器（VWAP/ATR/ORB）
    sum_pv_1m: float = 0.0
    sum_v_1m: float = 0.0
    vwap_1m: float = 0.0
    atr_1m: Optional[float] = None
    prev_close_1m: Optional[float] = None
    min_1m: float = math.inf
    max_1m: float = -math.inf
    close_1m: float = 0.0
    vol_1m: float = 0.0
    last_minute_ts: Optional[datetime] = None

    # ORB（PT 06:30 窗口，时区校正由上游保证）
    orb5: Optional[Tuple[float, float]] = None
    orb15: Optional[Tuple[float, float]] = None

    # 滚动/自适应
    abs_dev_90: Deque[float] = None
    updown_15: Deque[int] = None
    dev_15: Deque[float] = None

    q80_ewma: Optional[float] = None
    k_ewma: Optional[float] = 0.50  # Rev-F：策略/日内样本更新，这里仅持有

    # 滑动窗口缓冲（多分辨率）
    win_1s: Deque[MicroBar] = None
    win_5s: Deque[MicroBar] = None
    win_15s: Deque[MicroBar] = None

    # 发射点记忆（避免重复写出）
    last_emitted: Dict[str, datetime] = None  # gran -> ts

    def __post_init__(self):
        if self.abs_dev_90 is None: self.abs_dev_90 = deque(maxlen=90)
        if self.updown_15 is None: self.updown_15 = deque(maxlen=15)
        if self.dev_15 is None: self.dev_15 = deque(maxlen=15)
        if self.win_1s is None: self.win_1s = deque()   # 仅缓存最近 15 秒内需要的元素
        if self.win_5s is None: self.win_5s = deque()
        if self.win_15s is None: self.win_15s = deque()
        if self.last_emitted is None: self.last_emitted = {}

class SFSService:
    def __init__(
        self,
        symbols: List[str],
        emit_grans: Set[str] = {"1s", "5s", "15s"},
        tick_size: float = 0.01,
        lateness_sec: int = 3,          # 允许迟到窗口
        step_1s_for_all: bool = True,   # True=5s/15s 也按 1s 步长滑动；False=按各自粒度翻滚
    ):
        self.symbols = set(symbols)
        self.state: Dict[str, SymState] = defaultdict(SymState)
        self.emit_grans = emit_grans
        self.tick = tick_size
        self.lateness = timedelta(seconds=lateness_sec)
        self.step_1s_for_all = step_1s_for_all
        self._watermark: Optional[datetime] = None  # 事件时间水位线

    # ------------ 外部接口：不断喂入 1s 微 bar ------------
    async def ingest(self, bar: MicroBar):
        if bar.symbol not in self.symbols: return
        self._advance_watermark(bar.ts)
        st = self.state[bar.symbol]

        # 维护 1m 聚合（按 bar 所属分钟）
        minute_ts = bar.ts.replace(second=0, microsecond=0)
        if st.last_minute_ts is None:
            st.last_minute_ts = minute_ts
        if minute_ts > st.last_minute_ts:
            self._on_minute_close(st)    # 关闭上一分钟（更新 ATR/滚动/ORB）
            # 重置 1m 聚合器
            st.sum_pv_1m = 0.0; st.sum_v_1m = 0.0
            st.min_1m, st.max_1m = math.inf, -math.inf
            st.vol_1m = 0.0
            st.last_minute_ts = minute_ts

        # 累加 1m 聚合
        st.min_1m = min(st.min_1m, bar.low)
        st.max_1m = max(st.max_1m, bar.high)
        st.close_1m = bar.close
        st.vol_1m += bar.volume
        # VWAP 日内累加
        st.sum_pv_1m += bar.close * bar.volume
        st.sum_v_1m += bar.volume
        st.vwap_1m = st.sum_pv_1m / max(1e-9, st.sum_v_1m)

        # 放入各滑动窗口（1s/5s/15s）
        self._push_and_evict(st.win_1s, bar, window_sec=1)
        self._push_and_evict(st.win_5s, bar, window_sec=5)
        self._push_and_evict(st.win_15s, bar, window_sec=15)

        # 到达水位线可安全发射的 ts 列表
        for gran in self.emit_grans:
            for ts_emit in self._emit_points(gran, bar.ts, st.last_emitted.get(gran)):
                if ts_emit <= self._safe_ts():  # 仅对水位线之内的点发射
                    row = self._build_row(bar.symbol, gran, ts_emit, st)
                    await self._write_rows([row])
                    st.last_emitted[gran] = ts_emit

    # ------------ 核心：构造一行（窗口聚合 + 1m 快照） ------------
    def _build_row(self, symbol: str, gran: str, ts_emit: datetime, st: SymState) -> Dict:
        win = st.win_1s if gran == "1s" else st.win_5s if gran == "5s" else st.win_15s
        low, high, close, vol = self._agg_window(win, up_to=ts_emit)

        # regime（简化版，与 Rev-F 一致）
        p_up = sum(st.updown_15)/len(st.updown_15) if st.updown_15 else 0.5
        med_dev_15 = statistics.median(st.dev_15) if st.dev_15 else 0.0
        trend_push = (med_dev_15 / (st.atr_1m or 1e-9)) if med_dev_15 else 0.0
        if p_up >= 0.70 and trend_push >= 0.60: regime = 1
        elif p_up <= 0.30 and trend_push >= 0.60: regime = 2
        else: regime = 3

        return {
            "date": ts_emit.date().isoformat(),
            "symbol": symbol,
            "gran": {"1s":1, "5s":5, "15s":15}[gran],
            "ts": ts_emit,
            "low": low, "high": high, "close": close, "volume": vol,

            "sum_pv_1m": st.sum_pv_1m, "sum_v_1m": st.sum_v_1m,
            "vwap_1m": st.vwap_1m, "atr_1m": st.atr_1m or 0.0,
            "orb5_high": st.orb5[1] if st.orb5 else math.nan,
            "orb5_low":  st.orb5[0] if st.orb5 else math.nan,
            "orb15_high": st.orb15[1] if st.orb15 else math.nan,
            "orb15_low":  st.orb15[0] if st.orb15 else math.nan,

            "q80_ewma": st.q80_ewma or math.nan,
            "k_ewma": st.k_ewma or math.nan,
            "p_up_15": p_up,
            "trend_push_15": trend_push,
            "regime": regime,

            "ver": 1,
            "_ingest_ts": datetime.now(timezone.utc),
        }

    # ------------ 1m 关闭：ATR(Wilder)/滚动/ORB ------------
    def _on_minute_close(self, st: SymState):
        # ATR (Wilder)
        if st.prev_close_1m is None:
            st.prev_close_1m = st.close_1m
            st.atr_1m = max(st.max_1m - st.min_1m, 5*self.tick)
        else:
            tr = max(st.max_1m - st.min_1m,
                     abs(st.max_1m - st.prev_close_1m),
                     abs(st.min_1m - st.prev_close_1m))
            atr_raw = (st.atr_1m*13 + tr)/14 if st.atr_1m else tr
            atr_raw = max(atr_raw, 5*self.tick)
            st.atr_1m = min(atr_raw, (st.atr_1m or atr_raw)*1.5)  # 防跳变
            st.prev_close_1m = st.close_1m

        # 滚动
        vwap = st.vwap_1m
        st.abs_dev_90.append(abs(st.close_1m - vwap))
        st.updown_15.append(1 if st.close_1m >= vwap else 0)
        st.dev_15.append(abs(st.close_1m - vwap))

        # q80_ewma
        if st.abs_dev_90:
            xs = sorted(st.abs_dev_90)
            q80 = xs[int(0.80*(len(xs)-1))]
            st.q80_ewma = 0.70*q80 + 0.30*(st.q80_ewma if st.q80_ewma is not None else q80)

        # ORB（窗口 06:30–06:35 / 06:45）
        mt = st.last_minute_ts   # 上一根 1m 的时间
        if mt:
            hhmm = (mt.hour, mt.minute)
            if (hhmm >= (6,30)) and (hhmm < (6,36)):
                st.orb5 = (min(st.orb5[0], st.min_1m) if st.orb5 else st.min_1m,
                           max(st.orb5[1], st.max_1m) if st.orb5 else st.max_1m)
            if (hhmm >= (6,30)) and (hhmm < (6,46)):
                st.orb15 = (min(st.orb15[0], st.min_1m) if st.orb15 else st.min_1m,
                            max(st.orb15[1], st.max_1m) if st.orb15 else st.max_1m)

    # ------------ 滑动窗口工具 ------------
    def _push_and_evict(self, dq: Deque[MicroBar], bar: MicroBar, window_sec: int):
        dq.append(bar)
        bound = bar.ts - timedelta(seconds=window_sec)  # 严格窗口 (t-window, t]
        while dq and dq[0].ts <= bound:
            dq.popleft()

    @staticmethod
    def _agg_window(dq: Deque[MicroBar], up_to: datetime) -> Tuple[float,float,float,float]:
        lows, highs, vols, close = [], [], [], None
        for b in dq:
            if b.ts <= up_to:
                lows.append(b.low); highs.append(b.high); vols.append(b.volume); close = b.close
        if not lows:
            return math.nan, math.nan, math.nan, 0.0
        return min(lows), max(highs), close, sum(vols)

    # ------------ 发射点：滑动/翻滚（步长控制） ------------
    def _emit_points(self, gran: str, last_ts: datetime, last_emitted: Optional[datetime]) -> Iterable[datetime]:
        step = timedelta(seconds=1 if (self.step_1s_for_all or gran=="1s") else (5 if gran=="5s" else 15))
        start = (last_emitted + step) if last_emitted else last_ts.replace(microsecond=0)
        # 1s 对齐
        cur = start
        while cur <= last_ts.replace(microsecond=0):
            yield cur
            cur += step

    # ------------ 水位线（事件时间） ------------
    def _advance_watermark(self, evt_ts: datetime):
        # 简化：单分区/近实时；生产中请用每分区独立水位 + 全局 min
        self._watermark = max(self._watermark or evt_ts, evt_ts)

    def _safe_ts(self) -> datetime:
        return (self._watermark or datetime.min.replace(tzinfo=timezone.utc)) - self.lateness

    # ------------ 外部实现：批量写 CH ------------
    async def _write_rows(self, rows: List[Dict]):
        # TODO: 使用 clickhouse-driver / asynch 批量 INSERT
        # INSERT INTO features_sliding_exec (...columns...) VALUES
        pass

    # ------------ 示例 run 循环（占位） ------------
    async def run(self):
        while True:
            # bar = await self._poll_microbar_from_kafka()
            # await self.ingest(bar)
            await asyncio.sleep(0.001)
```

### 配置建议

```yaml
sfs:
  symbols: ["TSLA","NVDA","AAPL","AMD","QQQ","SPY"]
  base_input: "1s"          # tick→1s；若无1s可设 "5s"
  emit_grans: ["1s","5s","15s"]
  step_1s_for_all: true     # 5s/15s 也每秒滑动
  lateness_sec: 3
  tick_size: 0.01
  # IO：Kafka/CH 连接在你们现有 infra 里配置
```

---

## 4) 与策略的对接（Rev-F）

* **策略驱动**仍是 1m。
* 当某根 1m 触发入场后，从 `features_sliding_exec` 取本分钟内 **gran=5s（或1s）** 的子窗序列（`ORDER BY ts`），用 `low→high` 顺序喂给 `Position.on_subbar()`，即可稳定实现“**先 SL 后 TP/TRAIL**”。
* 缺 `1s/5s` 时：退化到 1m 的 `low→high`，仍满足语义但更保守。
* Rev-F 的 `vwap_1m / atr_1m / orb* / q80_ewma / regime` 已随行落库，策略侧直接用，**无需重复计算**。

---

## 5) 关键工程提示

* **幂等与迟到**：务必以 `(date,symbol,gran,ts)` 为 key 写 `ReplacingMergeTree`，迟到数据会自然覆盖。
* **资源与容量**：

  * 若 `emit_grans=1s,5s,15s & step_1s_for_all=true`，每支标的一天 ~ 6.5h × 3600 = 23,400 行/粒度 → 三粒度 ~ 70k/日；6 标的 ~ 420k/日，**完全可控**。
  * 若存储吃紧：把 15s 的步长设为 5s 或 15s。
* **回测一致性**：离线回放 1s 微 bar → 跑同一 SFS → 产出同表 → 策略读取同接口，**回测≈仿真≈实盘**。

---

### 一句话结论

用这套 SFS，**同一进程兼容 1s/5s/15s 滑动窗口**，并把 Rev-F 所需的 1m 基准指标与子窗执行路径统一提供给策略层；策略只负责做决策与执行状态机。
如果你愿意，我可以把 `_write_rows()` 换成实际的 ClickHouse 批量插入、加上 Kafka 消费与 Prometheus 监控样例（同文件继续扩展即可）。
