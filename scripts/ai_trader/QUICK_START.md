# AI Trader 快速启动指南

## 🚀 一键启动

```bash
cd .
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/auto_loop.py &
```

**这会启动自动守护进程，根据时间自动执行：**
- 07:00-09:30: `morning` (门控 + pending actions)
- 09:30-16:00: `intraday` (盘中交易)
- 16:00-20:00: `afterhours` (盘后交易)
- 20:00-07:00: `overnight` (夜间学习/HPO/RL)

---

## 📊 三种监控方式

### 1️⃣ **快速监控面板**（推荐，日常使用）
```bash
bash scripts/ai_trader/monitor.sh
```

**显示内容**:
- ✅ 进程状态（auto_loop、run_single_bot）
- ✅ 心跳状态（最近 5 分钟）
- ✅ 今日计划文件
- ✅ 最近日志（最后 10 行）
- ✅ 快捷命令提示

**使用场景**: 每次想快速确认系统是否在运行

---

### 2️⃣ **详细状态报告**（完整信息）
```bash
bash scripts/ai_trader/status.sh
```

**显示内容**:
- 进程信息（PID、CPU、内存、启动时间）
- 进程元数据（session、provider、symbols、interval、cycles）
- 心跳详情（完整 JSON）
- 计划文件（完整 JSON）
- ClickHouse 会话记录（今日所有会话）
- 最近错误日志

**使用场景**: 排查问题、查看详细运行历史

---

### 3️⃣ **数据库会话查询**（验证交易执行）
```bash
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/check_sessions.py
```

**输出示例**:
```
┌─ 📊 ClickHouse 会话（今日）
└────────────────────────────────────────────────────────────────────────────
Session    Bot ID                         Mode     Started              Symbols
----------------------------------------------------------------------------------------------------
3cb7241a   DEEPSEEK_NVIDIA_DEFAULT        PAPER    2025-10-29 03:24:35  TSLA
fb6b469e   DEEPSEEK_NVIDIA_DEFAULT        PAPER    2025-10-29 03:19:24  TSLA
454158cf   DEEPSEEK_NVIDIA_DEFAULT        PAPER    2025-10-29 03:16:49  TSLA
----------------------------------------------------------------------------------------------------
统计: 总计 3 个会话 | 运行中 0 | 已完成 1
```

**使用场景**: 确认交易是否真的在执行、查看历史会话

---

## 🔍 实时监控

### 方法 1: 持续刷新监控面板
```bash
watch -n 10 'bash scripts/ai_trader/monitor.sh'
```
每 10 秒自动刷新

### 方法 2: 实时日志
```bash
tail -f logs/deepseek_nvidia_*.log
```

### 方法 3: 实时心跳
```bash
watch -n 5 'cat runs/ai_trader/$(date +%Y-%m-%d)/hb/llm_*.json'
```

---

## ✅ 验证系统是否在工作

### 检查清单

#### 1. 进程运行？
```bash
ps aux | grep -E "auto_loop|run_single_bot" | grep -v grep
```
**预期**: 至少看到 1 个 `auto_loop.py` 进程

#### 2. 心跳更新？
```bash
ls -lh runs/ai_trader/$(date +%Y-%m-%d)/hb/
```
**预期**: 看到 `llm_*.json` 文件，且修改时间在最近 5 分钟内

#### 3. 计划文件更新？
```bash
ls -lh runs/ai_trader/$(date +%Y-%m-%d)/*_plan.json
```
**预期**: 文件存在，且修改时间在最近 5-10 分钟内

#### 4. 数据库有会话记录？
```bash
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/check_sessions.py
```
**预期**: 看到今日的会话记录

#### 5. 日志有交易决策？
```bash
grep "Cycle.*Complete" logs/deepseek_nvidia_*.log | tail -5
```
**预期**: 看到 "Cycle #N Complete" 输出

---

## 🛑 停止系统

### 优雅停止
```bash
pkill -f auto_loop.py
```

### 强制停止所有
```bash
pkill -f auto_loop.py && pkill -f run_single_bot.py
```

---

## 🔧 常见问题

### Q1: 看到 "⚠️ 无活跃心跳文件"
**原因**: 上一个交易周期已完成，等待下一个周期
**解决**: 这是正常的！auto_loop 每 5 分钟运行一次，耐心等待

### Q2: 进程在运行，但没有日志输出
**原因**: 可能在等待市场时间窗口
**检查**:
```bash
cat runs/ai_trader/$(date +%Y-%m-%d)/overnight_plan.json | jq .
```
看看计划配置

### Q3: "clickhouse-driver 未安装"
**解决**:
```bash
venv/bin/pip install clickhouse-driver
```

### Q4: 数据库查询失败
**检查**:
```bash
# 检查 ClickHouse 是否运行
ps aux | grep clickhouse-server

# 检查连接
venv/bin/python -c "from clickhouse_driver import Client; Client(host='localhost').execute('SELECT 1')"
```

---

## 📁 重要文件路径

### 配置
- **主配置**: `scripts/ai_trader/config/ai_trader.yaml`
- **策略配置**: `config.yml`（按 `strategies[*].enabled` 与 `learning_profile` 生效）

### 运行时数据
- **今日运行**: `runs/ai_trader/YYYY-MM-DD/`
- **心跳**: `runs/ai_trader/YYYY-MM-DD/hb/llm_*.json`
- **进程元数据**: `runs/ai_trader/YYYY-MM-DD/procs/llm_*.json`
- **计划**: `runs/ai_trader/YYYY-MM-DD/*_plan.json`
- **操作日志**: `runs/ai_trader/YYYY-MM-DD/tools_ledger.jsonl`

### 日志
- **LLM Trader**: `logs/deepseek_nvidia_*.log`
- **详细 API**: `logs/deepseek_nvidia_*_api_detailed.txt`

### 数据库
- **会话**: ClickHouse `llm.llm_sessions`
- **决策**: ClickHouse `llm.llm_decisions`
- **订单**: ClickHouse `llm.llm_orders`
- **成交**: ClickHouse `llm.llm_fills`

---

## 🎯 自动化特性

### 1. 自动 Watchdog（内置）
- 监控进程心跳，超时自动重启
- 配置: `scripts/ai_trader/config/ai_trader.yaml`
  ```yaml
  policies:
    tools_watchdog: true
    tools:
      llm_trader:
        max_runtime_min: 60
        heartbeat_timeout_s: 180
        max_restarts_per_day: 2
  ```

### 2. 时间段自动切换
- 07:00: 切换到 morning
- 09:30: 切换到 intraday
- 16:00: 切换到 afterhours
- 20:00: 切换到 overnight（学习/HPO/RL）

### 3. AI Brain 动态调整
- 每次运行都会重新评估健康状态
- 根据时间/风险动态调整 cycles 和参数
- 自动决定是否执行 HPO 上线、RL 启停

---

## 📚 进阶文档

- **完整监控指南**: `scripts/ai_trader/MONITORING_GUIDE.md`
- **系统设计**: `scripts/ai_trader/SYSTEM_DESIGN.md`
- **使用指南**: `scripts/ai_trader/HOW_TO_USE.md`
- **README**: `scripts/ai_trader/README.md`

---

## 💡 最佳实践

### 启动前
```bash
# 1. 健康检查
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/check_tools_health.py --critical-only

# 2. 确认基础设施运行
make up  # 或 cd infra && docker-compose up -d

# 3. 启动 AI Trader
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/auto_loop.py &
```

### 运行中
```bash
# 每 10 分钟检查一次
watch -n 600 'bash scripts/ai_trader/monitor.sh'

# 或在 tmux/screen 中实时跟踪日志
tail -f logs/deepseek_nvidia_*.log
```

### 盘后
```bash
# 查看详细状态
bash scripts/ai_trader/status.sh

# 查看日报
cat runs/ai_trader/$(date +%Y-%m-%d)/daily_report.md

# 查看会话统计
PYTHONPATH=$PWD venv/bin/python scripts/ai_trader/check_sessions.py
```

---

**最后更新**: 2025-10-29
**维护者**: Trading Team
