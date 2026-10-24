# QQQ 策略启动文档（09:25 ET 自动启动 run_system）

目标：每天美东时间 **09:25** 自动启动 `run_system.py`，用于 QQQ 策略调试/试跑。

## 目录结构

- 启动脚本：`scripts/run_system_autostart.sh`
- LaunchAgent：`ops/launchd/com.trading.run_system.0925.plist`
- 启动日志：
  - `logs/launchd_run_system.out.log`
  - `logs/launchd_run_system.err.log`

## 安装（只需一次）

在 macOS 上，LaunchAgent 需要放到用户目录：

```bash
mkdir -p ~/Library/LaunchAgents
cp ./ops/launchd/com.trading.run_system.0925.plist ~/Library/LaunchAgents/

# 重新加载（避免旧版本残留）
launchctl unload -w ~/Library/LaunchAgents/com.trading.run_system.0925.plist 2>/dev/null || true
launchctl load -w ~/Library/LaunchAgents/com.trading.run_system.0925.plist
```

说明：
- 定时是按**系统本地时区**执行；你的系统时间已是美东时间（ET），因此 `09:25` 即为 ET 的 `09:25`。

## 立即手动触发（不等 09:25）

```bash
launchctl kickstart -k gui/$UID/com.trading.run_system.0925
```

## 停用 / 卸载

停用：

```bash
launchctl unload -w ~/Library/LaunchAgents/com.trading.run_system.0925.plist
```

卸载（同时删除 plist 文件）：

```bash
launchctl unload -w ~/Library/LaunchAgents/com.trading.run_system.0925.plist 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.trading.run_system.0925.plist
```

## 如何确认是否已启动

查看进程：

```bash
pgrep -af ./run_system.py || true
```

查看 launchd 状态：

```bash
launchctl print gui/$UID/com.trading.run_system.0925 | head -n 60
```

查看启动日志：

```bash
tail -n 200 /workspace/Library/Logs/trading/launchd_run_system.out.log
tail -n 200 /workspace/Library/Logs/trading/launchd_run_system.err.log
```

## 启动行为说明

- `scripts/run_system_autostart.sh` 已升级为**单实例双保险**：
  - 先做原子锁（`runtime/locks/run_system_autostart.lock`），防止定时器和手动命令同时触发的并发启动。
  - 再检查 `run_system.py` 进程：
    - 若已运行，直接跳过本次启动。
    - 若未运行，使用仓库 venv：`venv/bin/python run_system.py` 启动。
  - 若出现陈旧锁（例如异常中断），脚本会在“无 `run_system` 存活 + 锁超过阈值”时自动回收锁。
- `TRADING_MODE` 解析顺序：显式环境变量优先，其次读取仓库 `.env`，最后才回退到 `shadow`。LaunchAgent 不再强制覆盖该值。

这意味着：即便你忘记关闭系统，**09:25 定时器不会再拉起第二个 `run_system`**。

## Kafka/Redis 健壮性（自愈说明）

- Tick 发布端启用了**有界队列**与**磁盘 spool**（配置见 `config.yml` → `services.market_data_service.tick_publisher`）：
  - Kafka 短暂不可用/网络波动时，会把 tick 先写入 `runtime/spool/tick_publisher/`，Kafka 恢复后自动回放。
  - spool 目录有大小上限（默认 `200MB`），超过会自动删除最旧分片（避免磁盘被占满）。
- SFS-1s 写 Redis 具备超时与熔断（配置见 `config.yml` → `services.sfs_1s.redis_*`）：
  - Redis 卡死/断线时不会拖垮事件循环；恢复后会自动重连继续写入。

## ATR 日终 JSON 缓存（次日预热兜底）

- 文件路径（可配置）：`runtime/state/qqq_intraday_atr_seed.json`
- 配置项（`config.yml` → `qqq_intraday_001.params.extras`）：
  - `atr_seed_path`
  - `atr_seed_keep_days`（默认保留最近 30 天）
- 自动写入时机：
  - 交易日 `15:59 ET` 后，策略会把 `TQQQ/SQQQ` 的 ATR 预热值写入该文件。
  - 策略关闭时也会尝试写一次。
- 自动读取时机：
  - 策略启动时、跨日重置时，会优先读取这个 JSON 作为 `ATR_pre` 兜底。
  - 如果当天没有新数据，也会回退到最近一个可用交易日记录。

### 手工回填（系统前一天没跑也可）

可直接手工写入下面这种最简格式（注意数值必须 > 0）：

```json
{
  "day": "2026-02-09",
  "TQQQ": 0.35,
  "SQQQ": 0.30
}
```

## 每分钟可观测性快照（低噪音）

- 文件路径（可配置）：`runtime/state/qqq_intraday_observability_qqq_intraday_001.json`
- 配置项（`config.yml` → `qqq_intraday_001.params.extras`）：
  - `enable_observability_file`（默认 `true`）
  - `obs_snapshot_path`
- 更新频率：策略循环内按分钟写一次（同一分钟只写一次，不刷终端日志）。

快照里包含以下关键字段，便于盘中和盘后核对：
- `ready` / `missing_required`：当前是否满足策略运行的必要数据条件。
- `required_values`：QQQ 1m close、QQQ VWAP、TQQQ/SQQQ last。
- `atr_source` + `atr5m14_live/pre/effective`：ATR 是实时值、预热值还是应急值。
- `age_sec`：各关键数据的新鲜度（秒），可快速判断是否断流/延迟。
- `strategy_state`：持仓、止损距离、锁参状态、deadwater/toxic_day 等。

也支持系统自动格式：

```json
{
  "latest": {
    "day": "2026-02-09",
    "atr_pre": {
      "TQQQ": 0.35,
      "SQQQ": 0.30
    }
  }
}
```
