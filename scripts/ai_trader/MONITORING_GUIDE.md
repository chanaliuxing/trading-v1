# AI Trader 监控指南

## 📊 监控架构总览

AI Trader 拥有**三层监控机制**：

### 1️⃣ **自动 Watchdog（内置）**
系统已经内置了自动监控和恢复机制。

**配置位置**: `scripts/ai_trader/config/ai_trader.yaml`
```yaml
policies:
  tools_watchdog: true     # 自动监控 llm_trader 进程
  tools:
    llm_trader:
      max_runtime_min: 60         # 最大运行时间 60 分钟
      heartbeat_timeout_s: 180    # 心跳超时 180 秒
      max_restarts_per_day: 2     # 每天最多重启 2 次
```

**工作原理**:
- 每次 `intraday_sequence()` 调用时，会自动扫描所有 llm_trader 进程
- 检查心跳文件（`runs/YYYY-MM-DD/hb/llm_*.json`）的更新时间
- 如果心跳超时或运行时间过长，**自动杀死进程**
- 如果重启次数未超限，**自动重启进程**
- 所有事件记录在 `runs/YYYY-MM-DD/tools_ledger.jsonl`

**关键文件**:
- **进程元数据**: `runs/YYYY-MM-DD/procs/llm_<PID>.json`
  - 包含 PID、启动时间、命令、重启次数等
- **心跳文件**: `runs/YYYY-MM-DD/hb/llm_<ID>.json`
  - 每个循环更新一次，包含 cycle_count、positions、timestamp
- **操作日志**: `runs/YYYY-MM-DD/tools_ledger.jsonl`
  - 记录所有 kill/restart 事件

**实现位置**: `scripts/ai_trader/orchestrator.py` (`_scan_and_recover_tools()`)

自 vNext 起，watchdog 具备“计划感知”：
- 重启前会校验进程是否仍在当日最新 `brain_plan_*.json` 中；不在计划内的进程将被标记为 `retired`，并不会重启。
- 在每次执行新计划后会进行一次主动对账（reconcile），立即停止不在当前计划内的进程。
- 额外新增 `metadata_ttl_min`（默认 120 分钟），对 `retired/exited/backoff/failed_restart` 的元数据做 TTL 清理。

---

### 2️⃣ **健康检查工具**
在系统启动前，验证所有依赖是否正常。

**使用方法**:
```bash
# 完整检查
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/check_tools_health.py

# 详细输出
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/check_tools_health.py --verbose

# 仅检查关键工具
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/check_tools_health.py --critical-only
```

**检查项目**:
- ✅ Brain Core（大脑核心）
- ✅ External（外部依赖：llm_trader、数据库）
- ✅ Reflection（反思工具链）
- ✅ Evaluators（评估工具）
- ✅ Context（上下文分析）
- ✅ Evolvers（进化工具）
- ✅ RL（强化学习）
- ⚠️  Portfolio（组合优化，可选）

**输出示例**:
```
🏥 Health Check Summary
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Brain Core: 4/4 passed
✅ External: 1/3 passed
✅ Reflection: 5/5 passed
✅ Evaluators: 1/1 passed
✅ Context: 1/1 passed
✅ Evolvers: 2/2 passed
✅ RL: 1/1 passed
⚠️  Portfolio: 1/2 passed
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total: 16/19 checks passed
```

**实现位置**: `scripts/ai_trader/check_tools_health.py`

---

### 3️⃣ **人工监控脚本（新增）**
提供实时监控面板和详细状态查询。

#### 📊 **快速监控面板**
```bash
bash scripts/ai_trader/monitor.sh
```

**显示内容**:
- 进程状态（auto_loop、run_single_bot）
- 心跳状态（最近 5 分钟内）
- 今日计划文件
- 最近日志（最后 10 行）
- 快捷命令提示

#### 📝 **详细状态报告**
```bash
bash scripts/ai_trader/status.sh
```

**显示内容**:
- 进程信息（PID、CPU、内存、启动时间）
- 进程元数据（session、provider、symbols、interval、cycles）
- 心跳详情（完整 JSON）
- 计划文件（完整 JSON）
- 数据库会话（最近 5 个）
- 最近错误日志（最后 5 行）

---

## 🔍 监控检查清单

### 日常检查（每天早上）
```bash
# 1. 快速健康检查
bash scripts/ai_trader/monitor.sh

# 2. 检查是否有失败的会话
cat runs/ai_trader/$(date +%Y-%m-%d)/tools_ledger.jsonl 2>/dev/null

# 3. 检查错误日志
grep -i "error\|exception" logs/deepseek_nvidia_*.log | tail -20
```

### 启动前检查
```bash
# 1. 工具健康检查
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/check_tools_health.py --critical-only

# 2. 系统健康门检查
PYTHONPATH=$PWD venv/bin/python docs/daily/system_health_check.py

# 3. 数据门检查（最近 10 分钟有 tick 数据）
clickhouse-client --query "SELECT count() FROM lake.ticks WHERE ts_recv > now() - INTERVAL 10 MINUTE"
```

### 盘中实时监控
```bash
# 1. 实时日志（推荐使用 tmux/screen）
tail -f logs/deepseek_nvidia_*.log

# 2. 实时心跳（每 5 秒刷新）
watch -n 5 'cat runs/ai_trader/$(date +%Y-%m-%d)/hb/llm_*.json'

# 3. 进程状态
watch -n 10 'bash scripts/ai_trader/monitor.sh'
```

### 异常响应
```bash
# 1. 查看 watchdog 操作日志
cat runs/ai_trader/$(date +%Y-%m-%d)/tools_ledger.jsonl | jq .

# 2. 检查重启次数
jq '.restarts' runs/ai_trader/$(date +%Y-%m-%d)/procs/llm_*.json

# 3. 手动停止所有进程
pkill -f auto_loop.py && pkill -f run_single_bot.py

# 4. 手动重启
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/auto_loop.py &
```

---

## 📁 关键文件路径

### 配置
- **主配置**: `scripts/ai_trader/config/ai_trader.yaml`
- **策略配置**: `config.yml`（按 `strategies[*].enabled` 与 `learning_profile` 生效）

### 运行时数据
- **今日运行目录**: `runs/ai_trader/YYYY-MM-DD/`
- **进程元数据**: `runs/ai_trader/YYYY-MM-DD/procs/llm_<PID>.json`
- **心跳文件**: `runs/ai_trader/YYYY-MM-DD/hb/llm_<ID>.json`
- **计划文件**: `runs/ai_trader/YYYY-MM-DD/*_plan.json`
- **操作日志**: `runs/ai_trader/YYYY-MM-DD/tools_ledger.jsonl`
- **决策记录**: `runs/ai_trader/YYYY-MM-DD/morning_decision*.json`
- **反思报告**: `runs/ai_trader/YYYY-MM-DD/reflection*.json`
- **日报**: `runs/ai_trader/YYYY-MM-DD/daily_report.md`

### 日志
- **LLM Trader 日志**: `logs/deepseek_nvidia_YYYYMMDD_HHMMSS.log`
- **系统日志**: `logs/trading_system.log`

### 数据库
- **会话记录**: ClickHouse `llm.llm_sessions`
- **决策记录**: ClickHouse `llm.llm_decisions`
- **订单记录**: ClickHouse `llm.llm_orders`
- **成交记录**: ClickHouse `llm.llm_fills`
- **记忆存储**: ClickHouse `ai_trader_memory_episodes`

---

## ⚙️ 配置调优

### Watchdog 参数调整

**场景 1: 日内高频交易（快速失败）**
```yaml
policies:
  tools_watchdog: true
  tools:
    llm_trader:
      max_runtime_min: 30          # 缩短至 30 分钟
      heartbeat_timeout_s: 60      # 缩短至 60 秒
      max_restarts_per_day: 5      # 增加至 5 次
```

**场景 2: 日内低频交易（宽容模式）**
```yaml
policies:
  tools_watchdog: true
  tools:
    llm_trader:
      max_runtime_min: 120         # 延长至 2 小时
      heartbeat_timeout_s: 300     # 延长至 5 分钟
      max_restarts_per_day: 2      # 保持 2 次
```

**场景 3: 开发/调试（禁用 watchdog）**
```yaml
policies:
  tools_watchdog: false            # 禁用自动杀进程
```

---

## 🚨 告警与通知（未实现，待扩展）

### 推荐集成方案

#### 1. Slack 通知
在 `orchestrator.py:_scan_and_recover_tools()` 添加：
```python
import requests

def _notify_slack(message: str):
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if webhook_url:
        requests.post(webhook_url, json={"text": message})

# 在 kill 进程时通知
_notify_slack(f"⚠️ Killed llm_trader (PID {pid}): {reason}")

# 在重启时通知
_notify_slack(f"🔄 Restarted llm_trader (PID {proc.pid})")
```

#### 2. Prometheus + Grafana
导出指标到 Prometheus：
- `ai_trader_process_count{session="morning|intraday|overnight"}`
- `ai_trader_heartbeat_age_seconds{process_id="..."}`
- `ai_trader_restarts_total{process_id="..."}`
- `ai_trader_cycles_total{process_id="..."}`

#### 3. Email 告警
在关键事件触发时发送邮件：
- 进程被杀死
- 超过最大重启次数
- 健康检查失败

---

## 📊 监控最佳实践

### 启动前
✅ 运行健康检查 (`check_tools_health.py`)
✅ 检查系统健康门 (`system_health_check.py`)
✅ 验证数据可用性（ClickHouse、Redis）

### 运行中
✅ 每 5 分钟检查一次监控面板 (`monitor.sh`)
✅ 实时跟踪日志（`tail -f logs/...`）
✅ 监控心跳文件更新时间
✅ 关注 `tools_ledger.jsonl` 是否有 kill/restart 事件

### 盘后
✅ 查看详细状态报告 (`status.sh`)
✅ 检查日报 (`daily_report.md`)
✅ 验证反思结果 (`reflection*.json`)
✅ 检查数据库会话记录

### 问题排查
1. **进程频繁重启** → 检查心跳超时配置、增加 `max_runtime_min`
2. **心跳停止更新** → 检查 LLM API 是否卡住、网络是否正常
3. **超过最大重启次数** → 检查日志找出根本原因、手动重启
4. **无计划文件** → 检查 orchestrator 是否报错、大脑是否启用

---

## 🛠️ 快捷命令速查表

```bash
# 启动
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/auto_loop.py &

# 监控
bash scripts/ai_trader/monitor.sh                    # 快速面板
bash scripts/ai_trader/status.sh                     # 详细状态
tail -f logs/deepseek_nvidia_*.log                   # 实时日志
watch -n 5 'cat runs/ai_trader/$(date +%Y-%m-%d)/hb/llm_*.json'  # 心跳

# 检查
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/check_tools_health.py --critical-only   # 工具健康
cat runs/ai_trader/$(date +%Y-%m-%d)/tools_ledger.jsonl | jq .  # 操作日志

# 停止
pkill -f auto_loop.py                                # 停止 auto_loop
pkill -f run_single_bot.py                           # 停止所有 bot
kill -9 <PID>                                        # 强制杀死

# 清理
rm -rf runs/ai_trader/$(date +%Y-%m-%d)/            # 删除今日运行数据
rm logs/deepseek_nvidia_*.log                        # 清理日志
```

---

## 📚 相关文档

- **系统设计**: `scripts/ai_trader/SYSTEM_DESIGN.md`
- **使用指南**: `scripts/ai_trader/HOW_TO_USE.md`
- **测试报告**: `scripts/ai_trader/TESTING_COMPLETE.md`
- **README**: `scripts/ai_trader/README.md`

---

**最后更新**: 2025-10-29
**维护者**: Trading Team
