# 当前系统使用说明（基于仓库现状）

更新时间：2026-04-11
定位：说明“现在怎么用”，不讨论未来改造。

## 1. 系统当前运行定位
- 默认交易模式：`paper`（来自 `.env` 的 `TRADING_MODE=paper`）。
- 运行主体：`run_system.py`（策略加载、风险校验、执行链路）。
- 当前配置中启用策略（`config.yml`）：
  - `qqq_intraday_001`
  - `qqq_intraday_option_v1_001`
  - `qqq_1dte_ma10_reject_0324_v2_001`
  - `spy_1dte_ma10_reject_0324_001`
  - `stocks_kernel_shadow_smh_001`

## 2. 启动与停止（人工运维）

### 2.1 一键启动（推荐）
- 命令：`scripts/start_system_one_click.sh`
- 行为：
  - 检查并拉起 Docker 基础服务
  - 检查 PostgreSQL
  - 检查 IBKR API 连通
  - 启动 `run_system.py`

### 2.2 最小启动
- 命令：`venv/bin/python run_system.py`
- 说明：依赖 `.env` 与基础设施已就绪。

### 2.3 自动守护启动（launchd）
- 脚本：`scripts/run_system_autostart.sh`
- 对应 plist：`ops/launchd/com.trading.run_system.0925.plist`
- 典型用途：09:25 ET 自动拉起并 KeepAlive。

### 2.4 停止
- 常用：`pkill -f run_system.py`

## 3. 每日自动化入口（现有）

### 3.1 全量日程安装
- 入口：`Start_Automation.command`
- 实际脚本：`scripts/setup_full_daily_automation.sh`
- 任务含：
  - 每小时健康检查
  - 盘前检查
  - premarket / market / overnight LLM 任务
  - 16:01 `recon-all`
  - 17:05 nightly optimize

### 3.2 数据与特征日更
- 入口：`make recon-all`
- 包含：多周期 reconciliation、nightly data refresh、`compute-features-all`、备份。

## 4. 候选生成相关（当前怎么跑）

### 4.1 夜间候选优化
- 主命令：`venv/bin/python scripts/super_win_nightly_optimize.py`
- 主要产物目录：`runs/ai_trader/<date>/super_win_nightly/`
  - `trials_summary.csv`
  - `best_result.json`
  - `data_readiness.json`

### 4.2 回放/验证产物
- 常见目录：`runtime/state/**`
- 常见文件：
  - `option_active_strategy_evidence_status.json`
  - `option_strategy_rollout_evidence_report.json`
  - `spy_option_ma_*parity*.json`
  - `qqq_state_shadow_acceptance/*`

## 5. 监控与日志

### 5.1 系统日志
- `logs/run_system.out.log`
- `logs/run_system.err.log`
- `logs/trading_system.log`（若启用对应写入）

### 5.2 状态快照
- `runtime/state/qqq_intraday_qqq_intraday_001.json`
- `runtime/state/qqq_intraday_observability_qqq_intraday_001.json`
- 其他策略 runtime/state 快照

### 5.3 健康检查
- 脚本：`docs/daily/system_health_check.py`
- 盘前/盘后：`docs/daily/morning_check.sh`、`docs/daily/evening_review.sh`

## 6. 人工介入点（当前真实情况）
- `run_system.py` 启停与异常恢复通常需要人工确认。
- 自动化任务安装/卸载需要人工执行 launchd 脚本。
- 部分优化流程默认不自动应用到生产配置（需显式开关/命令）。
- 候选上线仍依赖证据审阅与人工放行。

## 7. 当前“使用闭环”一句话
- 当前可实现：**人工启动系统 + 自动跑策略/任务 + 自动产出回测/验证证据 + 人工审核与放行**。
