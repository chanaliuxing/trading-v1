# Gatekeeper Signal Engine v1

> 信号工具（不交易执行）：自动拉取（IBKR）→ 缺失可手填 → 分步规则 → 输出信号卡片（含原因链）。

## v1 边界（避免跑偏）

- 只做信号输出，不做交易执行
- 不做新闻抓取/理解，用 `event_confirmed` 手动变量替代
- 仅手动点 Run，不做后台定时任务
- 数据策略：IB Gateway 优先，拉 1min 历史 bars + last price

## 运行

```bash
streamlit run tool/gatekeeper/app.py
```

## 快速自测（推荐）

```bash
# 刷新（拉 anchor + ticker 的价格/1m bars + VIX/VVIX）
python tool/gatekeeper/scripts/test_refresh.py --anchor QQQ --tickers META

# 期权数据（IVR / RR25 / ATM spread）
python tool/gatekeeper/scripts/test_options.py --symbol META

# 离线跑规则（不连 IBKR，用手动 override 的假数据验证 pipeline 逻辑）
python tool/gatekeeper/scripts/test_run_offline.py --symbol META --anchor QQQ --profile gatekeeper_v1_1
```

## IB Gateway 提示

- 默认连接参数在 `tool/gatekeeper/config.json`
  - `host=127.0.0.1`
  - `port=4002`（常见：IB Gateway 纸账户；实盘常用 `4001`）
  - `clientId=7`
- UI 顶部可修改 host/port/clientId，并点击 `Connect / Reconnect`

## 常见问题

- 期权 bid/ask/last 显示为 `NaN`：通常是连接状态/数据农场短暂异常，已做一次自动重连后重试；若仍不稳定可手动 `Connect / Reconnect` 再 `Refresh/Run`。

## v1 必填（缺失会 NO_SIGNAL）

- `spread_width_usd`（手动）
- 每个 ticker 的 `ivr` / `skew_value`（IBKR 获取不保证可用，v1 可直接手填）
- `bofa_9_3`（手动；`>=8` 触发 weekly_red）
