特斯拉策略1005

下面给你一份真正可落地、面向个人散户、尽量简洁的《TSLA（日内做多）策略 V7.2·简化稳健版》。
它把我们之前所有“容易踩坑”的点都做了防呆：不拍脑袋；参数=数据求解；“几天不触发”的场景用目标触发率+反饥饿限权解决；成本用L1 half-spread 统计约束；市场/组合风险有乘数+NDX 桶保护。你可以直接按本文档写成程序（若需要，我可把本文档转成参数 JSON+伪代码骨架）。
 
0. 使用者画像与边界
•	只做多、只做正股 TSLA（不做空、不做期权、不隔夜）。
•	目标：日内尽量有 3–6 次高质量机会；成本后保持正期望；笔记本可运行。
•	不保证每日必赚，但所有下单都要通过“成本后边际”硬门槛。
 
1. 数据与清洗（轻量、可落地）
输入
•	Bars：TSLA 1m/5m/60m（RTH 09:30–16:00 ET）。
•	L1：bid/ask/last/size（用于点差与可成交限价）。
•	参考：SPY、QQQ（5m），VIX（1m 或 5m）。
•	历史窗口：短窗 7 日（分位与触发率）、长窗 60 日（尾部/回撤/阈值稳定）、基线 180 日（收缩基线）。
清洗
•	仅 RTH；剔除 abs(ret_1m) > 25%、volume==0 的 bar。
•	ATR 用 Wilder/RMA；VWAP 为当日会话内累积；RVOL 用时间槽法（同一时刻对比近 5–7 天中位量）。
•	L1 质量评分（过去 60s）：更新频率、时延、负点差/交叉价占比。低于 30 日分布 P10 ⇒ 暂停新开 2 分钟（只许减/平）。
 
2. 盘前“参数大脑”（简化稳健版 v1.5）
目标：能用、稳；避免重型优化。全部是小候选集 + 30 日前瞻验证 + 收缩到基线。
再加“目标触发率反求”，避免“几天不触发”。
2.1 候选集（TSLA 专用）
•	市场噪声带 ε（SPY/QQQ, 5m, 60 日）：{P25, P30, P35}（取“更弱”的指数）。
•	极端暂停阈值
o	VIX 突增率 θ_vix ∈ {P90,P95}（60 日分布）。
o	点差 θ_spread ∈ {P98,P99}（7 日同时间槽 half-spread%）。
o	ATR 异动 θ_atr ∈ {P90,P95}（|ΔC_5m|/ATR_5m，60 日分布）。
o	暂停时长 t_pause ∈ {10,15} 分钟。
•	A1（VWAP 回归）
o	偏离阈值分位 thr_sigma_p ∈ {55,60,65,70,75}（基于当日残差 z-score 的历史分布）。
o	RVOL 窗：[P30,P80] 或 [P25,P75]（7 日时间槽）。
•	A2（ATR 网格）
o	步长 g = max(Pq(TR_5m), D_p80/L)，q ∈ {25,28,32}，L ∈ {3,4,5}。
o	单边过滤 oneway_thr = P90(|C−O|_5m)（7 日，EWMA21 平滑）。
•	B1（ORB 回踩）
o	ORB 时长 ∈ {30,45} 分钟；
o	rvol_min ∈ {0.82,0.85,0.88}（60 日分布分位）。
•	k_ATR（止损尺度）
o	k_ATR = λ × P90(|ΔC_5m|)/mean(ATR_5m)；λ ∈ {0.9,1.0,1.1}。
2.2 前瞻验证（Walk-Forward，轻量）
•	用最近 30 个交易日训练挑选第 31 日验证；滑动到昨天。
•	目标函数（固定 α、β，避免再拟合）：
[
Score=\text{Median}(\text{daily PnL}{net})-\alpha\cdot \text{Turnover}-\beta\cdot \text{DD}{95}
]
•	取Top-3 组合做 softmax 加权（连续参数取加权平均；离散参数多数票）。
2.3 收缩到长期基线（180 日）
•	稳定性分数 S = 1 - |p_t - p_{t-1}|/MAD(p)；
•	当日参数 p_today = w·p_t + (1−w)·p_base，w = clip(S, 0.5, 0.9)。
2.4 目标触发率（解决“几天不触发”）
•	设 T_target = 4（当日期望触发总次数）。
•	用 7 日统计的触发函数 H(p) 估算触发量：
o	若 H(p) < 0.5*T_target ⇒ 自动调松一级（A1 的 thr_sigma_p 下降一档；A2 的 g 下降 5%）。
o	若 H(p) > 2*T_target ⇒ 自动收紧一级（防过度交易）。
2.5 护栏 & 回退（Last-Known-Good）
•	硬边界（TSLA）
o	A1.thr_sigma ∈ [0.004,0.020]（即 0.4%–2.0% 偏离）。
o	A2.grid_step_g ∈ [0.6,3.5] 美元（或 ATR 倍数 [0.15,0.9]）。
o	k_ATR ∈ [1.4,3.0]；pause.theta_spread ∈ [0.0002,0.0012]。
o	A1.time_exit ∈ [25,60] 分；B1.rvol_min ∈ [0.75,1.10]。
•	体检失败或越界 ⇒ 回退到昨天参数（LKG），并做：k_ATR +10%、thr_sigma +5%、g +5%（更保守）。
•	冷启动自检（1 分钟）：用开盘前 5 分钟 L1 统计验证“成本后边际”与“可触发率≥50%×T_target”；不达标再微调一次，否则回退到 LKG。
 
3. 市场/组合守护（运行期，轻逻辑）
3.1 市场状态乘数（SPY/QQQ 取“更弱”）
•	δ = P/EMA50 - 1；阈带 ε 用盘前得到的值。
•	market_mult：BULL=1.00；NEUTRAL=0.75；BEAR=0.50。
3.2 暂停与熔断
•	Pause：ΔVIX/VIX_prev > θ_vix 或 half-spread% > θ_spread 或 |ΔC_5m|/ATR_5m > θ_atr ⇒ 暂停新开 t_pause 分钟。
•	Kill-Switch（硬）：|ΔC_1m| ≥ 3×ATR(14,1m) 或 LULD/撮合异常 或 L1 断流≥60s ⇒ 立刻平仓并停当日。
3.3 NDX 桶（防“QQQ+TSLA/NVDA 双押注”）
•	桶：{QQQ, NVDA, AAPL, MSFT, META, AMD, AVGO, GOOGL, TSLA}。
•	账户权益 E；组合当日损失上限 L_max = min(0.015E, P80(历史当日最大回撤))。
•	额度：
o	取 QQQ 60 日 5m 的 VaR_qqq_95；
o	cap_bucket = min(0.30E, L_max / VaR_qqq_95)；
o	cap_symbol = min(0.12E, 0.5×cap_bucket)；
o	并发上限 K ∈ {2,3}（盘前固定）。
•	相关性缩放（后进入者）：最近 5 日 5m 与 QQQ 相关 ρ ⇒ scale=max(0.25,1−ρ²) 乘到下单名义。
•	QQQ filler（可选）：若 30 分钟内 TSLA/NVDA 等 Alpha 标的无成交，可给 QQQ 一次小仓“填充”，额度再乘 filler_cap_ratio ∈ {0.3,0.5}。
 
4. 三个交易模块（TSLA 专属、只做多）
优先级：B1（趋势） > A1（回归） > A2（网格）。同刻多个信号只执行优先级最高的一个；另一个延后 5 分钟再评估。
4.1 A1｜VWAP 回归（1m）
•	入场（全满足）
1.	P < VWAP；
2.	偏离 dev = (P−VWAP)/VWAP ≥ thr_sigma（盘前求得，且 ≥ P40(dev) 下限）；
3.	RVOL ∈ [lo, hi]（盘前求得）；
4.	非 Pause。
•	下单：可成交限价 ask×(1+ε)；ε = clip(P95(half-spread%_{同时间槽,7日}), 0.02%, 0.08%)。
•	仓位：触发一次下 1 个基准单位（见 §5.1），不加仓。
•	止盈：到 VWAP 平 70%；余下跟踪 VWAP + 0.5×σ_res（σ_res = 7 日回归残差 std 的 EWMA21）。14:30 未达 ⇒ VWAP 附近清。
•	止损：stop = k_ATR × ATR(14,5m)（盘前求得）。
•	时间退出：T_exitA1 = clip(median(回到 VWAP 时长, 7 日), 25–60 分)。
4.2 A2｜ATR 网格（5m，锚=A=SMA30(5m)）
•	步长：g = max(Pq(TR_5m), D_p80/L)（盘前得 q、L）。
•	单边过滤：若 |C_now−O_0930| ≥ oneway_thr ⇒ 停止新增层（只等反抽减仓）。
•	建仓/加仓：P ≤ A − g 开第一层；每 g 再加 1 层；最多 4 层；份额 25%→20%→15%→10%。
•	止盈：回到 A 平 60%，余下 A+0.5g 清；14:30 未回到 A ⇒ A−0.25g ~ 打平 清。
•	止损/终止：若 |ΔC_5m|/ATR_5m > θ_atr 或“单边日”成立 ⇒ 停加层；已有仓止损 max(1.8, k_ATR)×ATR_5m，或反抽至 A−0.5g 先减半。
•	反饥饿：无成交 120/180 分钟 ⇒ g *= 0.9 / 0.8（但 ≥ P20(TR_5m)）。
4.3 B1｜ORB 回踩（5m）
•	ORB：默认 09:30–10:00；若近 7 日“趋势日命中率 <35%”，自动延至 10:15。
•	触发：close ≥ ORB_hi×1.001 且 RVOL ≥ rvol_min ⇒ 标记突破；随后 6 根 5m 内第一次回踩+收回（close ≥ ORB_hi×0.998）⇒ 做多。
•	止损：stop = max(P25(TR_5m), 0.35×ATR_5m)。
•	止盈：1R/2R 各 40%，余下用 5EMA(5m) 跟踪；close<5EMA 且 RVOL 放大 >1.2 ⇒ 出清。
•	时间退出：11:05（若 ORB=45min 则 11:20）未扩张 ≥ 2×P40(TR_5m) ⇒ 减半或清。
 
5. 仓位/执行/反饥饿/成本（简单强硬）
5.1 最终下单股数
•	账户权益 E；基准单位 base_unit_pct 由 60 日回测反推（通常 0.8%–1.5%）。
•	计算：
[
qty=\Big\lfloor \frac{E\times base_unit_pct}{P}\Big\rfloor
\times market_mult \times dd_mult
\quad \xrightarrow{\text{NDX 桶/相关性缩放}}
qty_{final}
]
5.2 当日回撤（策略内）
•	60 日当日最大回撤分布：soft=P70, hard=P85（不足用 2%/3%）。
•	达 soft ⇒ dd_mult=0.5；达 hard ⇒ 新开=0（只平）。
5.3 反饥饿 = “放宽有限权 + 探测单”
•	无成交 120/180 分钟：
o	A1：thr_sigma *= 0.9 / 0.8，RVOL 窗口外扩/内缩 5%/10%；
o	A2：g *= 0.9 / 0.8（下限 P20(TR_5m)）。
•	限权：放宽期间只打探测单（≤ 基准单位 50%），且新开名义再乘 0.5。
•	连续 3 张探测单净亏 ⇒ 自动收紧回原阈值。
•	放宽在第一笔盈利或14:00到点即关闭。
5.4 成本后边际（硬门槛）
•	滑点估计：近 7 日“同时间槽”的 half-spread P95；无样本则用 7 日全日 P95。
•	下单前必须满足：
[
\mathbb{E}[\text{gross}] - (\text{滑点}+\text{佣金}) \ge \text{edge_floor},
\quad \text{edge_floor}=\max(\text{P60(近7日单笔净盈亏)}, 0.02\times \text{ATR}_{5m})
]
•	失败 ⇒ 缩量或不下。
5.5 订单样式
•	开仓：可成交限价（买 ask×(1+ε)，卖 bid×(1−ε)），ε 取上面的 P95 区间并裁剪到 0.02%–0.08%。
•	止盈/止损：OCO；EOD 15:57 强平。
 
6. 每日 SOP（你实际要做的事）
•	08:30–09:15：加载 180/60/7 日数据 → 体检；候选集→30 日前瞻→Top-3 softmax→基线收缩→参数 JSON；做冷启动验收（不通过就微调/回退）。
•	盘中：1m 跑 A1/退出/成本门槛；5m 跑 A2/B1/Regime/Pause/NDX；15m 小幅再校准（只微调，不改结构）。
•	盘后：写日报（触发率/成本偏差/边际通过率/是否触发回退）；若当日 calibrator_status=OK，更新 LKG。
 
7. 这版为何能解决“连续几天不触发”？
1.	盘前用 T_target=4 反求阈值（不足就自动调松一级）。
2.	盘中有“反饥饿限权+探测单”，会小仓验证机会而不空等。
3.	仍没机会（低波动日）时，可开 QQQ filler 小仓一次（受 NDX 桶约束/相关性缩量）。
→ 在保持风控前提下，很难出现连续几天 0 触发。
 
8. 最小参数 JSON（你程序要读取）
数值由盘前脚本计算，下方只是字段与单位示例。
{
  "symbol": "TSLA",
  "asof": "YYYY-MM-DD",
  "windows": {"short_days":7, "long_days":60, "baseline_days":180},
  "target_triggers": 4,
  "regime": {"epsilon": 0.0048, "market_mult": {"BULL":1.0,"NEUTRAL":0.75,"BEAR":0.5}},
  "pause": {"theta_vix":0.12, "theta_spread":0.0007, "theta_atr":2.1, "t_pause_min":15},
  "k_atr": 2.0,
  "A1": {"thr_sigma":0.0092, "rvol_lo":0.85, "rvol_hi":1.8, "time_exit_min":40},
  "A2": {"grid_step_g":1.35, "oneway_thr":2.2, "max_layers":4},
  "B1": {"orb_len_min":30, "rvol_min":0.85, "pullback_window_M":6},
  "positioning": {"base_unit_pct":0.012, "cap_bucket":0.25, "cap_symbol":0.10, "max_concurrent":2, "filler_cap_ratio":0.5},
  "edge_floor": 0.18,
  "slippage": {"eps_p95":0.0005, "min":0.0002, "max":0.0008},
  "anti_starvation": {"L1_min":120, "L2_min":180, "relax_sigma":[0.9,0.8], "relax_g":[0.9,0.8], "probe_ratio":0.5, "revert_after_losses":3, "cutoff":"14:00"},
  "dd_limits": {"soft_pct":0.018, "hard_pct":0.028},
  "lkg": {"enabled": true}
}
 
结语（你要的“真正完整版本”）
•	可落地：指标、阈值、下单、风控、回退全给出；每项都写明“数值怎么从数据里算出来”。
•	轻量：不需要 L2 历史回放；全部 O(N) 指标+少量候选前瞻，笔记本轻松跑。
•	不拍脑袋：所有关键门槛来自分位/前瞻/基线收缩；还有硬护栏与回退防“毒参数”。
•	避免 0 触发：有目标触发率 + 反饥饿限权 +（可选）QQQ filler三重保障。
如果你愿意下一步就上机，我可以把盘前校准脚本的字段公式（每个 JSON 数值的精确计算流程）发给你，或直接给你最小可用的 Python 校准器（读本地 CSV/Parquet，产出 JSON），你把它接到你的交易引擎就能开跑。

