# QQQ 操作文档

本文档用于日常操作 `qqq_intraday_001` 策略，覆盖启动、盘中监控、复盘、故障排查。

## 值班版 1 分钟巡检清单

1. 单实例确认：`pgrep -af ./run_system.py`  
2. 策略进程确认：日志有 `qqq_intraday_initialized`。  
3. 快照文件确认：`runtime/state/qqq_intraday_observability_qqq_intraday_001.json` 存在且每分钟更新时间变化。  
4. 就绪状态确认：`ready=true`；若 `false`，先看 `missing_required`。  
5. 数据新鲜度确认：`QQQ/TQQQ/SQQQ` 的 `age_sec` 不持续升高。  
6. ATR 来源确认：优先 `live_5m`，次选 `atr_pre`；若 `fallback` 持续出现需排查。  
7. 锁参状态确认：09:45 后 `strategy_state.amp_locked=true`。  
8. 入场状态确认：看 `entry_gate`（未入场原因）或 `qqq_entered`（已入场）。  
9. 持仓风险确认：若已入场，检查 `stop_px`、`stop_dist_cur`、`break_even_armed`。  
10. 收盘后复盘：`python tools/replay_qqq_day.py --day YYYY-MM-DD`。

## 1. 策略目标与必需数据

策略依赖以下实时数据（ET 盘中）：

1. `QQQ` 1 分钟收盘价  
2. `QQQ` VWAP  
3. `TQQQ` / `SQQQ` 最新价  
4. `TQQQ` / `SQQQ` 5 分钟 ATR(14)（可用 `live / atr_pre / fallback`）

对应策略文件：`strategies/qqq_intraday_strategy.py`

## 2. 启动前检查

### 2.1 单实例检查

```bash
pgrep -af ./run_system.py || true
```

### 2.2 配置检查

确认 `config.yml` 中 `qqq_intraday_001` 已启用，且关键项正确：

- `rth_only: true`（上游行情服务配置）
- `fail_fast_enabled: false`（调试阶段）
- `enable_observability_file: true`
- `obs_snapshot_path: runtime/state/qqq_intraday_observability_qqq_intraday_001.json`
- `diag_log_path: runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl`
- `atr_seed_path: runtime/state/qqq_intraday_atr_seed.json`

## 3. 启动与停止

### 3.1 定时启动（推荐）

参考：`docs/qqq策略启动文档.md`  
默认 09:25 ET 启动，已做单实例保护。

### 3.2 手工启动

```bash
cd .
./venv/bin/python run_system.py
```

### 3.3 停止

```bash
pkill -f ./run_system.py
```

### 3.4 一键切换模式（盘后测试 / 盘中RTH）

盘后测试（全天可交易）：

```bash
cd .
./venv/bin/python tools/qqq_mode_switch.py test
```

盘中正式（仅RTH）：

```bash
cd .
./venv/bin/python tools/qqq_mode_switch.py intraday
```

查看当前模式：

```bash
./venv/bin/python tools/qqq_mode_switch.py status
```

## 4. 盘中观测（低噪音）

## 4.1 每分钟可观测快照文件（主入口）

文件：

`runtime/state/qqq_intraday_observability_qqq_intraday_001.json`

查看方式：

```bash
cat runtime/state/qqq_intraday_observability_qqq_intraday_001.json | jq .
```

重点字段：

- `ready`: 当前是否满足策略运行必需条件
- `missing_required`: 缺失项列表
- `required_values`: 当前核心数值（QQQ/TQQQ/SQQQ）
- `atr_source`: `live_5m` / `atr_pre` / `fallback` / `missing`
- `age_sec`: 数据新鲜度（秒）
- `strategy_state`: 持仓、止损、锁参状态

### 4.2 单次即时快照脚本（人工核对）

```bash
./venv/bin/python tools/qqq_required_snapshot.py --json
```

### 4.3 入场拒绝原因（关键）

策略会高频写入 `entry_gate` 事件到诊断文件（不刷主日志），说明当前能否入场以及具体原因。

- `decision=BLOCKED`：当前被拒绝
- `decision=ENTRY_ALLOWED`：通过门控，允许下单
- 常见 `reason`：`data_not_ready`、`atr_rank_skip`、`pre_0945_no_early_override`、`fast_drift_not_ready`

诊断文件（默认）：

`runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl`

查看最新：

```bash
tail -n 50 runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl
```

同一文件中还包含每分钟 `qqq_loop_heartbeat`，用于确认主循环持续运行。

`entry_gate` 关键字段（用于精确定位）：

- `reason`
- `decision`
- `direction` / `instrument` / `mode`
- `qqq_close_now` / `vwap_now`
- `v3m_price` / `v3m_signed`
- `atr_live` / `atr_seed` / `atr_eff`
- `vol_surge_ratio`
- `data_age_qqq_sec`

`qqq_loop_heartbeat` 字段：

- `strategy_id`
- `is_traded`
- `amp_locked`
- `deadwater`
- `toxic_day`
- `data_age_qqq_sec`
- `v3m_price`
- `vol_surge_ratio`
- `code_version`

### 4.4 版本一致性检查（必须）

策略启动日志会带 `code_version`（git short sha）。  
复盘前必须确认运行版本与当前代码一致：

```bash
git rev-parse --short HEAD
tail -n 20 runtime/state/qqq_intraday_diag_qqq_intraday_001.jsonl | rg "qqq_intraday_initialized|code_version"
```

若不一致，本次运行日志不作为策略参数结论依据。

## 5. ATR 机制说明

优先级：

1. `live_5m`：实时 5 分钟 ATR(14)  
2. `atr_pre`：预热/缓存 ATR  
3. `fallback`：09:45 后兜底值（TQQQ=0.35, SQQQ=0.30）

ATR 缓存文件：

`runtime/state/qqq_intraday_atr_seed.json`

## 6. 当天复盘

### 6.1 自动时间线复盘

```bash
python tools/replay_qqq_day.py --day 2026-02-09
python tools/replay_qqq_day.py --day 2026-02-09 --full
python tools/replay_qqq_day.py --day 2026-02-09 --json
```

### 6.2 生成日报报告（推荐）

```bash
cd .
./venv/bin/python tools/qqq_generate_report.py --day 2026-02-09 --stdout
```

默认输出文件：

`runs/reports/qqq_daily_report_qqq_intraday_001_2026-02-09.md`

常用参数：

```bash
./venv/bin/python tools/qqq_generate_report.py --day 2026-02-09 --from 09:30 --to 16:00
./venv/bin/python tools/qqq_generate_report.py --day 2026-02-09 --strategy-id qqq_intraday_001 --out runs/reports/my_report.md
```

### 6.3 关键事件检索

```bash
rg "qqq_amp_locked|qqq_entered|qqq_stop_widened|qqq_break_even_armed|qqq_flatten_submitted|EMERGENCY_ATR_ACTIVE" ~/grafana-stack/logs/trader.log
```

## 7. 常见问题与处理

### 7.1 没有生成可观测快照文件

检查顺序：

1. 配置是否开启 `enable_observability_file: true`  
2. 策略是否已启动并进入循环（看 `qqq_intraday_initialized`）  
3. 日志是否有 `qqq_intraday_loop_error` 或 `qqq_observability_write_failed`  
4. 路径是否可写：`runtime/state/`

### 7.2 一直不入场

优先看 `entry_gate` 的 `reason`，不是猜测阈值。  
同时核对观测文件中的：

- `missing_required`
- `required_values`
- `atr_source`
- `strategy_state.amp_locked`

### 7.3 全程 fallback ATR

说明实时 ATR 未形成。优先检查：

1. `TQQQ/SQQQ` 1s 数据是否连续  
2. 5m bar 数是否增长（`bars_5m_count`）  
3. `atr_seed` 是否加载成功

## 8. 推荐日常流程

1. 09:25 前确认单实例 + 配置  
2. 09:30~09:45 观察 `ready/missing_required`  
3. 09:46 后关注 `entry_gate` 与 `qqq_entered`  
4. 收盘后运行 `replay_qqq_day.py` 做日总结  
5. 必要时调整阈值并记录变更
