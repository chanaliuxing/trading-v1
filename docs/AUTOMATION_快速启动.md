# 三层防护体系 - 快速启动指南

**5 分钟激活自动化数据质量保障系统**

---

## ✅ 前置条件检查

```bash
cd .

# 1. 确认工具脚本存在
ls -lh tools/check_active_completeness.py
ls -lh tools/gateway_watchdog.py
ls -lh scripts/automation/morning_health.sh

# 2. 确认 ClickHouse 运行中
docker ps | grep clickhouse
# 或
curl http://localhost:8123/ping

# 3. 确认 Python 环境正常
venv/bin/python --version
```

如果有任何文件缺失或 ClickHouse 未运行，请先解决这些问题。

---

## 🚀 快速启动 (3 步)

### Step 1: 测试 Layer A (开盘前检查)

```bash
# 测试运行 (不会影响系统)
./scripts/automation/morning_health.sh
```

**预期输出**:
```
================================================================
Morning Health Check - Enhanced with Data Integrity
================================================================
[System] Layer A: Pre-market Data Integrity Check & Auto-Seed
[Check] Running features_1m_active sync check...
[Success] ✅ Data Integrity Check Passed
         Features Active Table is synced (≥98% coverage)
================================================================
```

如果看到 `✅ Data Integrity Check Passed`，说明 Layer A 工作正常！

### Step 2: 启动 Layer B (实时监控)

```bash
# 后台启动 Watchdog
nohup venv/bin/python tools/gateway_watchdog.py > logs/watchdog.log 2>&1 &

# 保存 PID (用于后续停止)
echo $! > /tmp/watchdog.pid

# 确认启动成功
tail -10 logs/watchdog.log
```

**预期输出**:
```
================================================================================
[Watchdog] Gateway Watchdog Started
[Watchdog] Monitoring: runs/ai_trader/2025-11-25/ml_gateway_decisions.jsonl
[Watchdog] Symbols: TSLA,NVDA,SPY,AAPL,MSFT,META,AMD,GOOGL,QQQ,AMZN
[Watchdog] Cooldown: 300s per symbol
[Watchdog] Circuit Breaker: 10 repairs/hour
================================================================================
```

### Step 3: 激活 Crontab (自动化)

```bash
crontab -e
```

添加以下两行:
```cron
# Layer A: 每天早上 8:30 运行健康检查 (工作日)
30 8 * * 1-5 cd . && ./scripts/automation/morning_health.sh >> logs/morning_health.log 2>&1

# Layer B: 开机自启动 Watchdog (可选，如果不用 systemd)
@reboot cd . && nohup venv/bin/python tools/gateway_watchdog.py > logs/watchdog.log 2>&1 &
```

保存退出 (`:wq`)。

验证:
```bash
crontab -l
```

---

## 🧪 验证系统工作

### 验证 Layer A

```bash
# 查看今天的健康检查日志
tail -50 logs/morning_health.log
```

### 验证 Layer B

```bash
# 查看 Watchdog 是否运行
ps aux | grep gateway_watchdog

# 查看监控日志
tail -f logs/watchdog.log
```

### 验证 Layer C (AI 工具)

```bash
# 手动测试 AI 诊断工具
venv/bin/python scripts/ai_trader/brain/tools_sql.py diagnose \
  --symbols TSLA \
  --date $(date +%Y-%m-%d) \
  --auto-fix
```

如果返回 JSON 结果，说明 Layer C 工作正常！

---

## 📊 日常监控

### 每日检查 (早上 10 点)

```bash
# 1. 确认 morning_health.sh 运行成功
grep "Success" logs/morning_health.log | tail -1

# 2. 确认 Watchdog 正在运行
ps aux | grep gateway_watchdog

# 3. 查看是否有修复记录 (如果有，说明系统自动处理了问题)
grep "watchdog_repair" logs/watchdog.log | tail -10
```

### 每周检查 (周五下午)

```bash
# 统计本周修复次数
grep "watchdog_repair_success" logs/watchdog.log | grep "$(date +%Y-%W)" | wc -l

# 查看是否触发熔断器 (如果有，说明有系统性问题)
grep "circuit_breaker" logs/watchdog.log | tail -5
```

---

## 🛑 如何停止

### 停止 Layer B (Watchdog)

```bash
# 使用保存的 PID
kill $(cat /tmp/watchdog.pid)

# 或手动查找
ps aux | grep gateway_watchdog
kill <PID>
```

### 停止 Layer A (Crontab)

```bash
crontab -e
# 注释掉或删除相关行
```

---

## 🔧 故障排查

### 问题 1: morning_health.sh 运行失败

**检查**:
```bash
# 手动运行并查看详细输出
./scripts/automation/morning_health.sh

# 如果报错 "permission denied"
chmod +x scripts/automation/morning_health.sh

# 如果报错 "ClickHouse connection failed"
docker ps | grep clickhouse
# 启动 ClickHouse
cd infra && docker-compose up -d clickhouse
```

### 问题 2: Watchdog 启动后立即退出

**检查**:
```bash
# 查看错误日志
cat logs/watchdog.log

# 常见原因 1: 日志目录不存在
mkdir -p runs/ai_trader/$(date +%Y-%m-%d)

# 常见原因 2: Python 模块缺失
venv/bin/pip install -r requirements.txt
```

### 问题 3: Crontab 不运行

**检查**:
```bash
# 确认 cron 服务运行
# macOS:
sudo launchctl list | grep cron

# Linux:
sudo systemctl status cron

# 查看 cron 日志
tail -f /var/log/syslog | grep CRON
```

---

## 📈 成功指标

**第 1 天**:
- [x] morning_health.sh 运行成功
- [x] Watchdog 启动并监控日志
- [x] AI 诊断工具测试通过

**第 1 周**:
- [x] 无需手动干预数据同步问题
- [x] Gateway invalid_features 错误减少 >90%
- [x] Watchdog 修复记录 <5 次/周 (说明预防有效)

**长期** (1 个月后):
- [x] 完全自动化，0 人工干预
- [x] Layer A 每日成功率 >95%
- [x] Layer B 修复时间 <5 秒
- [x] AI Brain 可以独立诊断和修复问题

---

## 🎯 下一步优化

**短期 (1 周内)**:
- [ ] 添加 Slack/Email 告警 (Layer A 失败时)
- [ ] 创建监控 Dashboard (Grafana)

**中期 (1 个月内)**:
- [ ] 添加 Prometheus 指标
- [ ] 实现 Layer B 的自适应冷却时间
- [ ] 优化 Layer A 只检查活跃 symbols

**长期 (3 个月内)**:
- [ ] AI Brain 主动预测数据质量问题
- [ ] 自动生成周报 (修复统计、根因分析)
- [ ] 跨系统集成 (DataHealer、FeatureBuild)

---

## 🆘 获取帮助

**文档**:
- 完整文档: `docs/AUTOMATION_三层防护体系.md`
- 诊断报告: `reports/DIAGNOSTIC_REPORT_空气列修复.md`
- 操作指南: `reports/NEXT_STEPS_验证与监控.md`

**日志位置**:
- Layer A: `logs/morning_health.log`
- Layer B: `logs/watchdog.log`
- Gateway: `runs/ai_trader/<date>/ml_gateway_decisions.jsonl`
- AI Brain: `runs/ai_trader/<date>/brain_tools/`

**紧急联系**:
- 系统出现异常循环修复 → 停止 Watchdog (kill)
- Morning check 持续失败 → 检查 ClickHouse 和 bars 数据源
- 所有工具都失效 → 查看 reports/DIAGNOSTIC_REPORT_空气列修复.md 手动修复流程

---

**准备好了吗？立即运行 Step 1-3，5 分钟激活系统！**

**维护者**: Trading Team
**最后更新**: 2025-11-25
