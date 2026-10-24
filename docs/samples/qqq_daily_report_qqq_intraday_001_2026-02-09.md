# QQQ 日报 - 2026-02-09

## 执行摘要
- strategy_id: `qqq_intraday_001`
- traded: `False`
- first_initialized: `2026-02-09T15:09:37.249450`
- first_amp_locked: `2026-02-09T09:45:01.761178`
- first_entered: `-`
- last_shutdown: `-`
- source_rows: `1034` (trader=964, diag=70)
- report_window_et: `09:30-16:00`
- order_or_fill_rows_found: `0`

## 交易结论
- verdict: `today no trade`（未检测到 `qqq_entered`，且无关联 order/fill）
- dominant_gate_block: `amp_not_locked` x `34`
- diagnosis: 观察窗口内 `amp_locked` gate 未通过
- run_gate_summary: run1[15:09:37]: base=False, 0945=True, amp=False; run2[15:24:36]: base=False, 0945=True, amp=False; run3[15:33:05]: base=False, 0945=True, amp=False
- likely_root_cause: 策略在 09:30 后重启，拿到 `price_0945` 但错过 09:30:00-09:30:10 的 price_base 窗口，后续无法锁定 amp

## 输入源
- trader_log: `/workspace/grafana-stack/logs/trader.log`
- diag_log: `runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl`
- state_file: `runtime/state/qqq_intraday_qqq_intraday_001.json`
- observability_file: `runtime/state/qqq_intraday_observability_qqq_intraday_001.json`

## 运行分段
| Run | 开始 | 结束 | Base Locked | 09:45 Locked | Amp Locked | Entered | Top Block |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 15:09:37 | 15:24:36 | False | True | False | False | - |
| 2 | 15:24:36 | 15:33:05 | False | True | False | False | - |
| 3 | 15:33:05 | 15:50:31 | False | True | False | False | - |

## 分钟级 Gate/Order 时间线
| ET分钟 | 数据状态 | Gate细节 | Gate Matrix | 订单/成交 | 未交易原因 |
| --- | --- | --- | --- | --- | --- |
| 09:30 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:31 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:32 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:33 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:34 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:35 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:36 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:37 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:38 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:39 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:40 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:41 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:42 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:43 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:44 | missing=['TQQQ:atr5m14', 'SQQQ:atr5m14'] | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 09:45 | - | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 15:09 | - | - | amp=-<br>vwap=-<br>add=-<br>dev=-<br>atr=-<br>qty=- | 无 | 无 gate 日志 |
| 15:24 | is_traded=False<br>amp_locked=False<br>qqq_age=11.7s<br>add_age=12.7s | amp_not_locked<br>long_ok=None<br>short_ok=None<br>vel=-<br>th=0.00<br>dev=0.00001<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=FAIL<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:25 | is_traded=False<br>amp_locked=False<br>qqq_age=35.8s<br>add_age=36.8s | amp_not_locked<br>long_ok=None<br>short_ok=None<br>vel=-<br>th=0.00<br>dev=0.00001<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=FAIL<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:26 | is_traded=False<br>amp_locked=False<br>qqq_age=1.9s<br>add_age=2.9s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=-<br>th=0.00<br>dev=0.00001<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=FAIL<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:27 | is_traded=False<br>amp_locked=False<br>qqq_age=2.1s<br>add_age=2.1s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=-<br>th=0.00<br>dev=0.00040<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=FAIL<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:28 | is_traded=False<br>amp_locked=False<br>qqq_age=2.3s<br>add_age=4.3s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=7.00<br>th=0.00<br>dev=0.00009<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:29 | is_traded=False<br>amp_locked=False<br>qqq_age=1.5s<br>add_age=2.5s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=2.50<br>th=0.00<br>dev=0.00045<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:30 | is_traded=False<br>amp_locked=False<br>qqq_age=1.7s<br>add_age=2.7s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=7.50<br>th=0.00<br>dev=0.00010<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:31 | is_traded=False<br>amp_locked=False<br>qqq_age=1.9s<br>add_age=2.9s | amp_not_locked<br>long_ok=False<br>short_ok=True<br>vel=-3.50<br>th=0.00<br>dev=0.00014<br>dev_th=0.00000<br>atr=0.140<br>sym=SQQQ | amp=FAIL<br>vwap_trend=PASS<br>add_velocity=PASS<br>deviation=PASS<br>atr=PASS<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:32 | is_traded=False<br>amp_locked=False<br>qqq_age=2.1s<br>add_age=2.1s | amp_not_locked<br>long_ok=False<br>short_ok=True<br>vel=1.50<br>th=0.00<br>dev=0.00043<br>dev_th=0.00000<br>atr=0.141<br>sym=SQQQ | amp=FAIL<br>vwap_trend=PASS<br>add_velocity=FAIL<br>deviation=PASS<br>atr=PASS<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:33 | is_traded=False<br>amp_locked=False<br>qqq_age=11.5s<br>add_age=11.5s | amp_not_locked<br>long_ok=None<br>short_ok=None<br>vel=-<br>th=0.00<br>dev=0.00012<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=FAIL<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:34 | is_traded=False<br>amp_locked=False<br>qqq_age=1.7s<br>add_age=2.7s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=-<br>th=0.00<br>dev=0.00002<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=FAIL<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:35 | is_traded=False<br>amp_locked=False<br>qqq_age=1.9s<br>add_age=4.9s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=-<br>th=0.00<br>dev=0.00029<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=FAIL<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:36 | is_traded=False<br>amp_locked=False<br>qqq_age=2.1s<br>add_age=4.1s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=-0.50<br>th=0.00<br>dev=0.00005<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:37 | is_traded=False<br>amp_locked=False<br>qqq_age=1.2s<br>add_age=2.2s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=-0.50<br>th=0.00<br>dev=0.00052<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:38 | is_traded=False<br>amp_locked=False<br>qqq_age=1.4s<br>add_age=2.4s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=-0.50<br>th=0.00<br>dev=0.00006<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:39 | is_traded=False<br>amp_locked=False<br>qqq_age=1.6s<br>add_age=1.6s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=-4.00<br>th=0.00<br>dev=0.00029<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:40 | is_traded=False<br>amp_locked=False<br>qqq_age=1.8s<br>add_age=1.8s | amp_not_locked<br>long_ok=True<br>short_ok=False<br>vel=-8.50<br>th=0.00<br>dev=0.00061<br>dev_th=0.00000<br>atr=0.112<br>sym=TQQQ | amp=FAIL<br>vwap_trend=PASS<br>add_velocity=FAIL<br>deviation=PASS<br>atr=PASS<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:41 | is_traded=False<br>amp_locked=False<br>qqq_age=2.0s<br>add_age=3.0s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=-5.50<br>th=0.00<br>dev=0.00038<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:42 | is_traded=False<br>amp_locked=False<br>qqq_age=2.2s<br>add_age=3.2s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=-5.00<br>th=0.00<br>dev=0.00009<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:43 | is_traded=False<br>amp_locked=False<br>qqq_age=1.4s<br>add_age=3.4s | amp_not_locked<br>long_ok=True<br>short_ok=False<br>vel=4.50<br>th=0.00<br>dev=0.00057<br>dev_th=0.00000<br>atr=0.113<br>sym=TQQQ | amp=FAIL<br>vwap_trend=PASS<br>add_velocity=PASS<br>deviation=PASS<br>atr=PASS<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:44 | is_traded=False<br>amp_locked=False<br>qqq_age=1.6s<br>add_age=1.6s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=4.00<br>th=0.00<br>dev=0.00042<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:45 | is_traded=False<br>amp_locked=False<br>qqq_age=1.8s<br>add_age=3.8s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=4.00<br>th=0.00<br>dev=0.00034<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:46 | is_traded=False<br>amp_locked=False<br>qqq_age=2.0s<br>add_age=2.0s | amp_not_locked<br>long_ok=True<br>short_ok=False<br>vel=-3.00<br>th=0.00<br>dev=0.00046<br>dev_th=0.00000<br>atr=0.107<br>sym=TQQQ | amp=FAIL<br>vwap_trend=PASS<br>add_velocity=FAIL<br>deviation=PASS<br>atr=PASS<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:47 | is_traded=False<br>amp_locked=False<br>qqq_age=2.2s<br>add_age=3.2s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=-10.00<br>th=0.00<br>dev=0.00038<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:48 | is_traded=False<br>amp_locked=False<br>qqq_age=1.4s<br>add_age=1.4s | amp_not_locked<br>long_ok=True<br>short_ok=False<br>vel=2.00<br>th=0.00<br>dev=0.00058<br>dev_th=0.00000<br>atr=0.110<br>sym=TQQQ | amp=FAIL<br>vwap_trend=PASS<br>add_velocity=PASS<br>deviation=PASS<br>atr=PASS<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:49 | is_traded=False<br>amp_locked=False<br>qqq_age=1.6s<br>add_age=1.6s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=0.50<br>th=0.00<br>dev=0.00040<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:50 | is_traded=False<br>amp_locked=False<br>qqq_age=1.8s<br>add_age=1.8s | amp_not_locked<br>long_ok=True<br>short_ok=False<br>vel=2.00<br>th=0.00<br>dev=0.00053<br>dev_th=0.00000<br>atr=0.110<br>sym=TQQQ | amp=FAIL<br>vwap_trend=PASS<br>add_velocity=PASS<br>deviation=PASS<br>atr=PASS<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:51 | is_traded=False<br>amp_locked=False<br>qqq_age=2.0s<br>add_age=2.0s | amp_not_locked<br>long_ok=True<br>short_ok=False<br>vel=-4.00<br>th=0.00<br>dev=0.00065<br>dev_th=0.00000<br>atr=0.107<br>sym=TQQQ | amp=FAIL<br>vwap_trend=PASS<br>add_velocity=FAIL<br>deviation=PASS<br>atr=PASS<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:52 | is_traded=False<br>amp_locked=False<br>qqq_age=2.2s<br>add_age=3.2s | amp_not_locked<br>long_ok=True<br>short_ok=False<br>vel=-4.00<br>th=0.00<br>dev=0.00085<br>dev_th=0.00000<br>atr=0.111<br>sym=TQQQ | amp=FAIL<br>vwap_trend=PASS<br>add_velocity=FAIL<br>deviation=PASS<br>atr=PASS<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:53 | is_traded=False<br>amp_locked=False<br>qqq_age=1.4s<br>add_age=1.4s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=0.00<br>th=0.00<br>dev=0.00044<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:54 | is_traded=False<br>amp_locked=False<br>qqq_age=1.6s<br>add_age=3.6s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=-1.50<br>th=0.00<br>dev=0.00026<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:55 | is_traded=False<br>amp_locked=False<br>qqq_age=1.8s<br>add_age=1.8s | amp_not_locked<br>long_ok=False<br>short_ok=True<br>vel=8.50<br>th=0.00<br>dev=0.00032<br>dev_th=0.00000<br>atr=0.165<br>sym=SQQQ | amp=FAIL<br>vwap_trend=PASS<br>add_velocity=FAIL<br>deviation=PASS<br>atr=PASS<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:56 | is_traded=False<br>amp_locked=False<br>qqq_age=2.0s<br>add_age=2.0s | amp_not_locked<br>long_ok=False<br>short_ok=False<br>vel=4.50<br>th=0.00<br>dev=0.00021<br>dev_th=0.00000<br>atr=-<br>sym=- | amp=FAIL<br>vwap_trend=FAIL<br>add_velocity=PASS<br>deviation=PASS<br>atr=FAIL<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |
| 15:57 | is_traded=False<br>amp_locked=False<br>qqq_age=2.2s<br>add_age=3.2s | amp_not_locked<br>long_ok=False<br>short_ok=True<br>vel=7.00<br>th=0.00<br>dev=0.00034<br>dev_th=0.00000<br>atr=0.165<br>sym=SQQQ | amp=FAIL<br>vwap_trend=PASS<br>add_velocity=FAIL<br>deviation=PASS<br>atr=PASS<br>qty=PASS | 无 | 09:45 amp gate 未锁定 |

## 订单生命周期
| Stage | Count | First | Last |
| --- | --- | --- | --- |
| intent_submitted | 0 | - | - |
| broker_ack | 0 | - | - |
| partial_fill | 0 | - | - |
| full_fill | 0 | - | - |
| flatten | 0 | - | - |

## 入场 Gate 诊断
- `amp_not_locked`: 34

## 数据与基础设施信号
- required_data_missing(debug) 高频 payload：
- `["TQQQ:atr5m14", "SQQQ:atr5m14"]`: 882
- sfs_1s_redis_timeout_count: `0`

## 循环健康度
- heartbeat_count: `34`
- heartbeat_max_gap_sec: `65.444`
- heartbeat_gap_over_90s: `0`
- code_versions_seen: `241aa25e`

## 最终快照
- state.day: `2026-02-09`
- state.is_traded: `False`
- state.amp_locked: `False`
- state.price_base: `None`
- state.price_0945: `615.43`
- state.locked_target: `None`
- state.updated_at: `2026-02-09T15:32:54.883641-05:00`
- state.reason: `shutdown:engine_shutdown`
- obs.ts_now_et: `2026-02-09T15:57:00.161964-05:00`
- obs.ready: `True`
- obs.missing_required: `[]`

## 事件计数 Top
- `qqq_required_data_missing(debug)`: 882
- `qqq_loop_heartbeat`: 34
- `qqq_entry_blocked`: 34
- `qqq_atr_seed_loaded`: 15
- `qqq_atr_seed_persisted`: 14
- `✅ Strategy subscribed to tick data`: 12
- `qqq_intraday_warmup_5m_done`: 6
- `qqq_state_restored`: 5
- `qqq_intraday_initialized`: 5
- `qqq_price_0945_locked`: 4
- `Initiating graceful shutdown... (Press Ctrl+C again to force exit)`: 3
- `✅ System shutdown complete`: 3
- `Loading strategy`: 3
- `tick_subscribe_results`: 3
- `Strategy loaded`: 3
- `qqq_intraday_daily_reset`: 3
- `EMERGENCY_ATR_ACTIVE`: 2
- `qqq_price_base_locked`: 1
- `qqq_cross`: 1
- `qqq_amp_locked`: 1

## 关键时间点
- qqq_intraday_initialized: `2026-02-09T15:09:37.249450` (trader.log)
- qqq_state_restored: `2026-02-09T15:09:37.044892` (trader.log)
- qqq_intraday_daily_reset: `2026-02-09T15:09:37.255029` (trader.log)
- qqq_price_base_locked: `2026-02-09T09:30:11.805022` (trader.log)
- qqq_price_0945_locked: `2026-02-09T09:45:00.758990` (trader.log)
- qqq_amp_locked: `2026-02-09T09:45:01.761178` (trader.log)
- EMERGENCY_ATR_ACTIVE: `2026-02-09T09:45:00.758696` (trader.log)
- qqq_entry_blocked: `2026-02-09T15:24:36.710836-05:00` (diag)
- delta_initialized_to_amp_locked_sec: `-19475.488`

## 最近 20 条时间线
- `2026-02-09T15:49:30.388873` [trader.log] `qqq_atr_seed_loaded`
- `2026-02-09T15:49:30.389814` [trader.log] `qqq_atr_seed_loaded`
- `2026-02-09T15:49:30.390766` [trader.log] `qqq_atr_seed_persisted` reason=eod_snapshot
- `2026-02-09T15:50:00.827929-05:00` [diag] `qqq_loop_heartbeat`
- `2026-02-09T15:50:00.828669-05:00` [diag] `qqq_entry_blocked` reason=amp_not_locked
- `2026-02-09T15:50:31.933782` [trader.log] `qqq_state_restored`
- `2026-02-09T15:51:00.996235-05:00` [diag] `qqq_loop_heartbeat`
- `2026-02-09T15:51:00.999261-05:00` [diag] `qqq_entry_blocked` reason=amp_not_locked
- `2026-02-09T15:52:00.181483-05:00` [diag] `qqq_loop_heartbeat`
- `2026-02-09T15:52:00.181765-05:00` [diag] `qqq_entry_blocked` reason=amp_not_locked
- `2026-02-09T15:53:00.374237-05:00` [diag] `qqq_loop_heartbeat`
- `2026-02-09T15:53:00.374938-05:00` [diag] `qqq_entry_blocked` reason=amp_not_locked
- `2026-02-09T15:54:00.615899-05:00` [diag] `qqq_loop_heartbeat`
- `2026-02-09T15:54:00.616587-05:00` [diag] `qqq_entry_blocked` reason=amp_not_locked
- `2026-02-09T15:55:00.801374-05:00` [diag] `qqq_loop_heartbeat`
- `2026-02-09T15:55:00.802164-05:00` [diag] `qqq_entry_blocked` reason=amp_not_locked
- `2026-02-09T15:56:00.985884-05:00` [diag] `qqq_loop_heartbeat`
- `2026-02-09T15:56:00.986252-05:00` [diag] `qqq_entry_blocked` reason=amp_not_locked
- `2026-02-09T15:57:00.165007-05:00` [diag] `qqq_loop_heartbeat`
- `2026-02-09T15:57:00.165511-05:00` [diag] `qqq_entry_blocked` reason=amp_not_locked
