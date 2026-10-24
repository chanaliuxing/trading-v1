下面给你 **Gatekeeper v1.1（修改后完整版）“文字规则书”**——这是**人读得懂**的版本（不是 IF-01 编号版），但内容与我们刚刚对齐的 v1.1 规则**完全一致**：包含你补的 4 个关键补丁（IVR 硬作用位、Event=Medium、RR25/IVR 期限锚点、CSS<30 默认切 debit）。

---

# Gatekeeper v1.1（修改后完整版）文字规则书

## 目标（系统只做“给信号的规则引擎”，不自动交易）

每天/每周打开面板，你要看到的是：

* 今天允许做什么（Credit Carry / Debit / Hedge / Wait）
* 最稳的结构是什么（BPS / BCS / IC / Debit Call / Debit Put / Wait）
* 为什么（可解释、可复现）
* 哪些关键参数需要你手动输入（缺数据就提示）

> 系统不会按顺序“自动试策略”，只会给 **Recommended + 0–2 alternatives**。

---

# 0) 输入与优先级（统一口径，避免实现乱跳）

## 0.1 手动输入优先（最高优先级）

任何字段只要你在 UI 手动填过，并标记为 `manual override`：

* 系统以后不再自动获取该字段
* 直到你清空 override，才恢复自动取数

---

## 0.2 Event（事件级别）统一输入

Event 统一为三档：

* `None`：无关键宏观/财报事件
* `Medium`：有重要事件（如 CPI/PPI/NFP/讲话等），**提醒但不否决**
* `High`：重大事件日/周，**可以一票否决 Carry（卖方）**

你提供的快捷逻辑：

* `EventConfirmed = Yes` → 当作 `High`
* `EventConfirmed = No` → 当作 `None`
  （但 UI 仍允许你直接选 Medium）

---

## 0.3 RR25（Skew）统一口径（写死）

> **RR25 = (IV_put25 − IV_call25) * 100**
> 输出为点数：`3.5 / 6.8 / 9.2`（禁止出现 0.07/7 混用）

**到期选择规则（写死）**：

* 选 `DTE ∈ [25, 45]`
* 优先 Monthly（第三个周五）
* 在候选链里取 **最接近 30D** 的那条链
* Put25/Call25 用 **delta 最接近 ±0.25** 的合约

---

## 0.4 IVR（IV Rank）统一口径（写死）

IVR 统一使用：

* **IV30 的 52 周 IV Rank**
* 即：当前 IV30 在过去 52 周 IV30 分布中的位置（0–100）

> 这条写死是为了保证 IC gating 稳定、不乱跳。

---

## 0.5 Width（价差宽度）必须手动输入

你明确要求：

* 系统可以建议 DTE / delta，但 **width_usd 不能“按你习惯”**
* 所以 v1.1 里：`spread_width_usd` **必须手动输入**
* 没有 width → 系统只能输出 WAIT + 提示你补输入

---

## 0.6 VWAP 定义与 Bias 口径（写死）

**VWAP 唯一公式**：

VWAP = sum(Price_i * Volume_i) / sum(Volume_i)

**Price_i（写死）**：

* 使用 HLC3：`(High + Low + Close) / 3`

**时间窗口（写死）**：

* **RTH 09:30–现在**（美东）
* 非交易时段：用上一交易日 RTH 全日 VWAP（最新可用 session）

**Bias 规则（写死）**：

* distance = (LastPrice - VWAP) / VWAP
* distance > +0.15% → Bullish
* distance < -0.15% → Bearish
* 其余 → Neutral

**边界说明**：

* VWAP 只决定 Bias
* 不参与 CSS/Skew/结构选择
* 允许手动 override（手动值优先于自动抓取）

---

# 1) Step 1：先画“安全空间”（只生成 Allowed / Forbidden，不选结构）

系统先汇总约束来源：

* Event（法律层）
* Skew（期权结构约束）
* BofA（只做 defensive overlay，不做禁令）
* 未来你可能加的 gate（例如流动性/收盘前等）

输出：

* `Forbidden`（不能做的结构集合）
* `Allowed`（可做结构集合）
* 以及 overlay 标记：`event_defensive_overlay` / `bofa_defensive_overlay` 等

---

## 1.1 Event 规则（None/Medium/High）

* **Event = High**

  * `forbid_credit = True`（禁止所有卖方：BPS/BCS/IC 全部不允许）
  * 同时提示：重大事件周
* **Event = Medium**

  * 不禁止 credit（`forbid_credit = False`）
  * 但开启：`event_defensive_overlay = True`
  * 并且追加一条硬行为：**IC 不进入 Recommended（最多当 alternative）**
* **Event = None**

  * `forbid_credit = False`
  * 不开启 event overlay

---

## 1.2 Skew（RR25）只输出 constraints（不改 Mode/Bias）

先分标的组：

* 指数：QQQ / SPY
* 高波个股：NVDA / TSLA / META

### 指数（QQQ/SPY）

* **RR25 > 7**：Extreme Put Skew
  → 禁卖 Put：**禁止 BPS**；并且 **IC 不允许/不推荐**（v1.1 用“禁 IC”来避免拆腿复杂）
* **RR25 5–7**：Elevated
  → 不禁结构，只打标：`skew_put_elevated=True`（后面用于更保守的参数）
* **RR25 < 3**：Complacent
  → 不动作，只提示（保险便宜/可能肉少）

### 高波（META/TSLA/NVDA）

* **RR25 > 6**：Extreme Put Skew
  → 禁卖 Put：禁止 BPS（v1.1 可选：也禁 IC，避免拆腿）
* **RR25 < -2**：Call FOMO
  → 禁卖 Call：禁止 BCS（v1.1 可选：也禁 IC）
* **-2 ~ +2**：Flat
  → 只输出偏好：`prefer_ic=True`（IC 更合适，但不是强制）

> 关键点：Skew 永远不决定做不做，只决定“哪边不能卖”。

---

## 1.3 BofA（只做 defensive overlay，不做禁令）

* **BofA ≥ 8**

  * 不禁结构
  * 只开启：`bofa_defensive_overlay=True`
  * 含义：必须用更远 delta、更小 size、更防守执行；并在解释里提示“更偏 bearish carry”
* 其余区间可选：轻度提示或不处理

---

# 2) Step 2：如果 Event=High（forbid_credit=True）直接走“买方/空仓池”

当 `forbid_credit=True`：

* 任何卖方结构都不允许（BPS/BCS/IC 全禁）
* 推荐只从：`Debit` / `Long option` / `Wait` 里选
* Bias 仍由价格决定：

  * Bullish → Debit Call Spread
  * Bearish → Debit Put Spread
  * Neutral → Wait/Flat

---

# 3) Step 3：如果 Market Mode ≠ CARRY，CSS 不参与

当 Market Mode 不是 Carry（Crash/Hedge/Directional/Wait）：

* CSS 不计算、不用于否决
* 系统只按 mode 输出对冲/买方/空仓建议

---

# 4) Step 4：只有在 Event≠High 且 Mode=CARRY 时，CSS 才介入

前提：Event=None 或 Medium，且 Mode=CARRY

CSS 的职责只有：

1. Carry 值不值得做（仅对卖方）
2. 做得多保守（size/delta）

CSS 分档（可调但 v1.1 默认）：

* CSS < 30：NO_CARRY（否决的是 Carry，不否决交易）
* 30–50：DEFENSIVE_CARRY（允许 carry，但必须防守）
* ≥50：正常/略激进 carry（只影响参数）

### 关键补丁：CSS<30 默认推荐切 debit（按 bias）

当 CSS < 30：

* **Bias=Bullish → Recommended=Debit Call Spread**
* **Bias=Bearish → Recommended=Debit Put Spread**
* **Bias=Neutral → Recommended=WAIT/FLAT**
  （alternatives 仍可给 1–2 个）

---

# 5) Step 5：Best Structure Selector（结构选择闭环：Bias + Skew + IVR）

前提：Event≠High + Mode=CARRY + CSS ≥ 30（允许 carry）

## 5.1 理想结构由 Bias 决定

* Bullish → 理想结构=BPS
* Bearish → 理想结构=BCS
* Neutral → 理想结构=IC

## 5.2 IC 的硬门槛（IVR gating）

* **IC 只有在 IVR ≥ 20 才允许进入 Recommended**
* **IVR < 20：IC 不推荐（最多 alternative）**

  * Neutral + IVR<20 → Recommended=WAIT
  * alternatives：给单边 spread（若允许）+ debit

## 5.3 Event=Medium 对 IC 的限制

* **Event=Medium：IC 不进入 Recommended（最多 alternative）**
  （即使 IVR ≥ 20 也一样）

---

# 6) Step 6：当理想结构被禁（constraints 冲突）时怎么做

原则：**不做自动反向 carry 流水线**（避免僵化）

当理想结构不在 Allowed：

* 推荐：WAIT/FLAT（告诉你：你当前 bias 的卖方执行不可行）
* alternatives：

  * 同方向 debit（bull→debit call；bear→debit put）
  * 另一个 carry（若允许）可以列为 alternative（供你手动选择），但不当 recommended

当理想结构在 Allowed：

* 推荐：理想结构
* alternatives：从 Allowed 里给 1–2 个合理备选（不按顺序试错）

---

# 7) Step 7：参数生成（所有 overlay 只影响 aggressiveness，不改结构类型）

前提：推荐不是 WAIT/FLAT

1. 基础模板生成建议参数（DTE、delta_range）
2. `spread_width_usd` 必须来自手动输入（缺则提示并 WAIT）
3. 应用防守覆盖层（叠加）：

* CSS 30–50：size_factor*=0.5；delta 更远
* Event=Medium overlay：size_factor 再降；delta 更远；且 IC 已被剔除推荐
* BofA≥8 overlay：对 BPS/IC-put 强制更远 delta + 更小 size；BCS 可正常或轻防守
* RR25 5–7（指数 elevated）：轻度更防守

---

# 8) 缺数据策略（防假信号）

如果 RR25、IVR、VIX/VVIX 等关键字段拿不到（且没有手动 override）：

* 系统不做“硬禁令”
* 直接提示 `need_manual_inputs`
* Recommended=WAIT/FLAT（避免缺输入导致“突然禁这禁那”）

---

## 最终结果你会在 UI 看到什么（示例）

* Event: Medium（手动）
* BofA: 9.3（手动）
* Mode: Carry
* Bias: Bullish
* RR25: 6.2（自动）
* IVR: 18（自动）
* CSS: 28（自动）

系统输出：

* ✅ Carry 被否决（CSS<30），但不否决交易
* ✅ Recommended: Debit Call Spread
* ⚠️ IC 不推荐（IVR<20 且 Event=Medium）
* overlays：event_defensive + bofa_defensive
* need_manual_inputs：若缺 width → 提示填 width 才给出完整参数

---

1) CSS 到底怎么算？（给你可编程公式 + 数据源 + 为什么这样算）
CSS 的定位（先一句话定性）

CSS = “今天做卖方 Carry（收租）到底划不划算 + 风险是不是可控”的评分。
它只在 Mode=CARRY 时生效，输出只有两件事：

carry_gate: NO_CARRY / DEFENSIVE / NORMAL / AGGRESSIVE

aggressiveness: size_factor、delta_overlay（更远/更近）

关键：CSS 不决定结构类型（BPS/BCS/IC 仍由 Bias + constraints 决定），也不吃 Skew 的惩罚（否则双重惩罚）。

v1.1 推荐 CSS 公式（简单、稳定、好写、可缓存）

CSS 由 3 个分量组成：Premium（肉多不多）+ Stability（会不会突然炸）+ Liquidity（能不能顺滑进出）。
分数 0–100，越高越适合做 Carry。

(A) PremiumScore（权利金“厚度”）：只用 IVR

数据：IVR = IV30 的 52W IV Rank（0-100）

映射（分段硬规则，避免过拟合）：

IVR	PremiumScore
< 20	0.20（肉太薄）
20–50	0.60（一般）
50–80	1.00（最舒服）
> 80	0.55（肉很厚但往往是事故期，不能给满分）

这样做的好处：你不会因为 “IVR=95 很香” 就被系统诱导去卖方硬扛崩盘。

(B) StabilityScore（尾部/跳空风险“体感”）：优先用 VVIX；没有就用 IV Jumpy

数据源优先级（v1.1 写死）：

指数/大盘环境：VVIX（最好）

若 VVIX 拿不到：用你已有的 IV 敏感度/开盘 15min jumpy 指标（ΔIV vs ΔPrice）

VVIX 映射：

VVIX	StabilityScore
≤ 95	1.00
95–105	0.75
105–115	0.45
>115	0.20

IV Jumpy（备用）：
定义一个非常好算的 ratio（你写代码很快）：

price_move_pct = abs((P_now - P_open) / P_open) * 100

iv_move_pts = abs(IV_ATM_now - IV_ATM_open) * 100（把 0.16 这种转成 16 点）

jumpy = iv_move_pts / max(price_move_pct, 0.10) （防止除 0）

映射：

jumpy	StabilityScore
< 2.0	1.00
2.0–4.0	0.70
4.0–6.0	0.45
> 6.0	0.20

这跟你之前的“跌不多但 IV 暴涨”是一脉相承的：越 jumpy 越不适合卖方。

(C) LiquidityScore（期权交易摩擦）：只看 ATM 附近点差

数据：ATM 附近期权 bid/ask（你用 IBKR 很容易拿到）

指标：spread_pct = (ask - bid) / mid * 100

spread_pct	LiquidityScore
≤ 0.8%	1.00
0.8–1.5%	0.70
1.5–2.5%	0.40
> 2.5%	0.20
CSS 总公式（写死，v1.1 用它）
CSS = round( 100 * (
  0.45 * PremiumScore +
  0.35 * StabilityScore +
  0.20 * LiquidityScore
))
CSS = clamp(CSS, 0, 100)

CSS 分档（你规则书里的阈值继续用）

< 30 → NO_CARRY（但不否决交易，默认切 debit/或 wait）

30–50 → DEFENSIVE_CARRY（只调参数：更远 delta + 更小 size）

>= 50 → NORMAL/AGGRESSIVE（只调参数）

重要声明：Skew 不参与 CSS（避免双重惩罚）

你问的“Skew 既做 constraints 又做 CSS*0.5 会不会双重惩罚？”——会。
所以 v1.1 我建议你写死：Skew 永远只产出 constraints/偏好，不影响 CSS。
否则系统会变成“又禁结构 + 又降分”，太容易把你逼成空仓机器。

2) RR25 / IVR：v1.1 该自动算还是手动？（我直接给你最优落地方案）
我的最终建议（v1.1 就做“自动为主 + 手动兜底”）

RR25、IVR v1.1 就自动算，但必须支持：

manual override（你手动填就永远用手动）

auto missing fallback（自动取不到就提示你填，不乱禁）

原因很简单：你最终诉求就是——输入 META，系统自动拉齐所有值给你 verdict。
如果 RR25/IVR 还必须手动，那你的“输入 ticker 自动分析”体验会被打断。

哪些字段 v1.1 必须支持手动输入（写死）

为了最快落地、又不出事故，我建议 v1.1 明确这些字段：

必填手动（系统不替你猜）：

spread_width_usd（你已经拍板必须手动）

Event level（默认你手动选；后面再加自动抓日历）

可选手动 override（自动有但你可覆盖）：

IVR

RR25

CSS（你也可以强行手动给分，系统照用）

BofA

必须自动（否则没意义）：

Price / VWAP（至少 1m/5m）

ATM IV / option bid-ask（否则 RR25/CSS 没法跑）

VIX/VVIX（能取就取，取不到就用 jumpy 备用）

自动计算的“实现成本分级”（给你实际工程建议）

IVR 自动：非常容易（一次拉 1Y 日线 IV，日更缓存）✅

RR25 自动：稍复杂但可控（选 DTE 25–45，找 delta≈±0.25 的 put/call，取 IV）✅

最省事写法：RR25 每天缓存一次（每个 ticker 一天算一次够用），面板刷新只读缓存+必要时刷新。

3) 文档/版本：要不要 v1.1 profile？保留 legacy 吗？（我直接定你该怎么做）
我的最终建议（工程最稳）

以 Gatekeeper_Signal_Engine_v1.md 作为唯一真源（source of truth）

新增：v1.1 profile（你现在这套规则）
