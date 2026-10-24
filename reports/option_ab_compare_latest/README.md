# Option A vs A+B 回测结果（可直接打开）

- `summary.csv`: A 单路径 vs A+B 双路径 汇总（真实期权链回放）
- `daily_compare.csv`: 两版本逐日对比（日期、交易数、日收益、差值）
- `trades_A.csv`: A 单路径逐笔明细
- `trades_AplusB.csv`: A+B 双路径逐笔明细

## MDD 收敛测试（只做参数微调）
- `grid_summary.csv`: 小步参数网格结果（net_pnl, mdd, win_rate）
- `best_pick.txt`: 基线参数 vs 选中参数（同引擎对比）
- `best_daily.csv`: 选中参数逐日收益
- `best_trades.csv`: 选中参数逐笔明细
