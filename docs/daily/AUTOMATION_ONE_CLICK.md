# 一键自动化使用说明（macOS）

> 一次安装，按时间自动运行四时段流程；所有任务内置“健康门”gating，`logs/system_health.json` 非 healthy 自动跳过。

---

## 1) 适用环境
- macOS（使用 launchd 定时）
- 项目根目录：`.`
- 已安装并可用：Python venv（`venv/`），`.env` 中 LLM/DB/IBKR 配置

---

## 2) 一键启用

方法 A（双击）：
- 在 Finder 中双击根目录下的 `Start_Automation.command`

方法 B（命令行）：
```bash
cd .
bash scripts/setup_full_daily_automation.sh
```

安装后自动创建/加载以下定时任务（本地时间）：
- 每小时整点：健康检查（更新 `logs/system_health.json`）
- 07:00：盘前健康/数据/执行门（`docs/daily/morning_check.sh`）
- 07:30：盘前 LLM（`llm_trader/run_premarket.sh`，内部 `--wait-for-premarket`）
- 09:30：盘中 LLM（`llm_trader/run_all_providers.sh market` 或 fallback 第一小时策略）
- 16:01：对账 + 特征计算 + 备份（`make recon-all`）
- 16:30：盘后复盘（`docs/daily/evening_review.sh`）
- 17:05：AI Trader 学习/参数评估（`python scripts/ai_trader/cli.py learn`）
- 20:00：隔夜 LLM（严格 `--close-at "03:50"`）

所有任务在执行前会先运行 `docs/daily/system_health_check.py`，只有当退出码为 0（healthy）时才继续。

---

## 3) 查看状态与日志
- 查看任务：`launchctl list | grep trading`
- 查看日志：`tail -f ./logs/*.log`
  - 示例：`logs/premarket.log`, `logs/market.log`, `logs/evening_review.log`, `logs/overnight.log`, `logs/health_cron.log`, `logs/recon_daily.log`

---

## 4) 一键停用

方法 A（双击）：
- 在 Finder 中双击根目录下的 `Stop_Automation.command`

方法 B（命令行）：
```bash
cd .
bash scripts/uninstall_full_daily_automation.sh
```

该脚本会卸载/删除所有以 `com.trading.*` 命名的 launchd 任务（健康/盘前/盘中/盘后/隔夜/备份）。

---

## 5) 常见问题
- 权限提示：首次运行 `.command` 文件如受限，执行 `chmod +x Start_Automation.command Stop_Automation.command`。
- 环境变量：cron/launchd 不读取你的 shell 配置，脚本已自动 `source .env` 与激活 `venv`。
- 非 macOS：改用 crontab（见 `docs/daily/AUTOMATION_TOOLS_SUMMARY.md` 里的 cron 示例），或让我们生成 systemd 单元文件。

---

## 6) 相关脚本（自动生成/使用）
- 安装：`scripts/setup_full_daily_automation.sh`
- 卸载：`scripts/uninstall_full_daily_automation.sh`
- 封装（含健康门/venv/.env）：`scripts/automation/*.sh`
