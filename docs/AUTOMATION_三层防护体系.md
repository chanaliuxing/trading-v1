# 三层防护体系 - 自动化数据质量保障

**目的**: 从"手动修复"升级到"自动预防 + 自动自愈"，彻底解决 `invalid_features` 问题

**架构**: 分层防御 (Layered Defense System)
- **层次 A**: 开盘前主动体检 + 自动补种 (Prevention)
- **层次 B**: 盘中实时监控 + 秒级自愈 (Self-Healing)
- **层次 C**: AI Brain 智能诊断工具 (Intelligence)

**状态**: ✅ 已实施，待激活

---

## 架构图

```
┌──────────────────────────────────────────────────────────────┐
│                    三层防护体系                               │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  Layer A: 开盘前体检                                          │
│  ├─ morning_health.sh (每天 08:30 cron触发)                  │
│  ├─ 检查 features_1m_active 同步状态                          │
│  ├─ 自动修复覆盖率 <98% 的 symbols                            │
│  └─ 修复失败 → 阻止系统启动                                   │
│                                                              │
│  Layer B: 盘中实时自愈                                         │
│  ├─ gateway_watchdog.py (后台守护进程)                        │
│  ├─ 监控 ml_gateway_decisions.jsonl                          │
│  ├─ 检测 [data_integrity] 错误                               │
│  ├─ 触发 auto-seed (5分钟冷却 + 熔断保护)                     │
│  └─ 修复完成 → Gateway 下次读取就正常                         │
│                                                              │
│  Layer C: AI Brain 诊断工具                                   │
│  ├─ diagnose_data_integrity() - 检查同步状态                  │
│  ├─ check_feature_coverage() - 找出空气列                     │
│  └─ 当 A/B 层失效时，AI 自主分析根因并给建议                   │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

---

## 层次 A: 开盘前主动体检

### 功能

**时机**: 每天开盘前 (09:25 或更早)
**作用**: 防止"带病上岗" - 确保启动时数据 100% 就绪
**文件**: `scripts/automation/morning_health.sh`

### 工作流程

1. **智能日期选择**:
   - 早上 9 点前 → 检查昨天数据
   - 9 点后 → 检查今天数据

2. **自动检查 + 修复**:
   ```bash
   check_active_completeness.py --auto-seed-active
   ```

3. **失败处理**:
   - 修复成功 → 继续启动
   - 修复失败 → 阻止启动 + 发送告警

### 激活方式

**方式 1: 手动运行** (测试用)
```bash
cd .
./scripts/automation/morning_health.sh
```

**方式 2: Crontab 定时** (生产推荐)
```bash
crontab -e
```

添加:
```cron
# 每天 08:30 运行 (市场开盘前 1 小时)
30 8 * * 1-5 cd . && ./scripts/automation/morning_health.sh >> logs/morning_health.log 2>&1
```

**方式 3: 集成到启动脚本**

如果你有自己的启动脚本 (如 `start_trading.sh`)，在启动 AI Brain 之前调用:
```bash
./scripts/automation/morning_health.sh || exit 1  # 检查失败直接退出
./scripts/start_ai_brain.sh  # 你的实际启动脚本
```

### 查看日志

```bash
# 实时查看
tail -f logs/morning_health.log

# 查看最近结果
tail -50 logs/morning_health.log | grep -E "(Success|CRITICAL|Coverage)"
```

---

## 层次 B: 盘中实时自愈

### 功能

**时机**: 系统运行期间（后台守护进程）
**作用**: 1 秒内检测并修复，AI Brain 甚至不会察觉
**文件**: `tools/gateway_watchdog.py`

### 工作流程

1. **实时监控**:
   - `tail -F` 跟踪 `ml_gateway_decisions.jsonl`
   - 检测关键词: `[data_integrity]`, `invalid_features`

2. **智能触发**:
   - 冷却机制: 同一 symbol 5 分钟内最多修复 1 次
   - 熔断器: 1 小时内最多修复 10 次（防止死循环）

3. **自动修复**:
   ```bash
   check_active_completeness.py --auto-seed-active --symbols <SYMBOL>
   ```

4. **结果日志**:
   - 修复成功 → 记录到 `logs/watchdog.log`
   - 修复失败 → 错误日志 + 继续监控

### 启动方式

**方式 1: 手动启动** (测试用)
```bash
cd .

# 前台运行 (Ctrl+C 停止)
venv/bin/python tools/gateway_watchdog.py

# 后台运行
nohup venv/bin/python tools/gateway_watchdog.py > logs/watchdog.log 2>&1 &

# 查看进程
ps aux | grep gateway_watchdog
```

**方式 2: Systemd 服务** (生产推荐)

创建 `/etc/systemd/system/gateway-watchdog.service`:
```ini
[Unit]
Description=Gateway Watchdog - Real-time Data Integrity Monitor
After=network.target

[Service]
Type=simple
User=w
WorkingDirectory=.
ExecStart=./venv/bin/python tools/gateway_watchdog.py
Restart=always
RestartSec=10
StandardOutput=append:./logs/watchdog.log
StandardError=append:./logs/watchdog.log

[Install]
WantedBy=multi-user.target
```

启动:
```bash
sudo systemctl daemon-reload
sudo systemctl enable gateway-watchdog
sudo systemctl start gateway-watchdog
sudo systemctl status gateway-watchdog
```

**方式 3: 集成到启动脚本**
```bash
# 在 start_trading.sh 中添加
nohup venv/bin/python tools/gateway_watchdog.py > logs/watchdog.log 2>&1 &
echo $! > /tmp/watchdog.pid  # 保存 PID 用于停止
```

### 环境变量配置

```bash
# 自定义监控的 symbols (默认: TSLA,NVDA,SPY,AAPL,MSFT,META,AMD,GOOGL,QQQ,AMZN)
export WATCHDOG_SYMBOLS=TSLA,NVDA,SPY
venv/bin/python tools/gateway_watchdog.py
```

### 查看日志

```bash
# 实时监控
tail -f logs/watchdog.log

# 查看修复历史
grep "watchdog_repair" logs/watchdog.log | tail -20

# 统计修复次数
grep "watchdog_repair_success" logs/watchdog.log | wc -l
```

### 停止 Watchdog

```bash
# 如果用 PID 文件
kill $(cat /tmp/watchdog.pid)

# 如果用 systemd
sudo systemctl stop gateway-watchdog

# 手动查找并停止
ps aux | grep gateway_watchdog
kill <PID>
```

---

## 层次 C: AI Brain 诊断工具

### 功能

**时机**: 当 A/B 层都失效时，AI 自主调用
**作用**: 深度分析根因，给出人类可读的建议
**文件**:
- `ai_brain/tools_manifest.json` (工具定义)
- `scripts/ai_trader/brain/tools_sql.py` (工具实现)

### 可用工具

#### 1. diagnose_data_integrity()

**用途**: 检查 features_1m_active 同步状态

**参数**:
```python
{
  "symbols": ["TSLA", "SPY"],  # 要检查的 symbols
  "date": "2025-11-20",        # 日期 (或 "today")
  "auto_fix": true,            # 是否自动修复
  "min_coverage": 0.95         # 最小覆盖率阈值
}
```

**返回**:
```json
{
  "ok": true,
  "symbols": ["TSLA", "SPY"],
  "date": "2025-11-20",
  "coverage_results": [
    {"symbol": "TSLA", "bars": 389, "active": 563, "coverage": 1.0},
    {"symbol": "SPY", "bars": 389, "active": 689, "coverage": 1.0}
  ],
  "repairs_applied": true,
  "recommendation": "All symbols have ≥95% coverage. System is healthy."
}
```

**AI Brain 使用场景**:
```
User: "Why is Gateway blocking TSLA trades?"

AI Brain:
1. 读取 ml_gateway_decisions.jsonl
2. 发现: decision="blocked", block_reason="[data_integrity] invalid_features: vwap, ema_10"
3. 调用: diagnose_data_integrity(symbols=["TSLA"], auto_fix=True)
4. 返回: "Coverage was 44%, auto-seed increased to 100%"
5. 回复用户: "Issue fixed. TSLA features_1m_active was out of sync, now repaired."
```

#### 2. check_feature_coverage()

**用途**: 找出哪些特征是空气列 (0% 覆盖)

**参数**:
```python
{
  "symbols": ["TSLA"],
  "days": 5,
  "table": "trading.features_1m_active"
}
```

**返回**:
```json
{
  "ok": true,
  "air_columns": ["vix_spread", "corr_spy_20"],  # 完全空的特征
  "partial_columns": ["beta_spy_20"],            # <90% 覆盖的特征
  "recommendation": "Found 2 air columns: vix_spread, corr_spy_20. Check feature_build pipeline."
}
```

**AI Brain 使用场景**:
```
User: "Which features are causing the invalid_features error?"

AI Brain:
1. 调用: check_feature_coverage(symbols=["TSLA"], days=5)
2. 发现: air_columns=["vix_spread", "corr_spy_20"]
3. 回复: "2 features are completely missing: vix_spread and corr_spy_20.
   These are likely cross-asset features that require SPY or VIX data.
   Check if those symbols are being processed by feature_build pipeline."
```

### 手动测试工具

```bash
cd .

# 测试诊断工具
venv/bin/python scripts/ai_trader/brain/tools_sql.py diagnose \
  --symbols TSLA SPY \
  --date 2025-11-20 \
  --auto-fix

# 测试特征覆盖检查
venv/bin/python scripts/ai_trader/brain/tools_sql.py coverage \
  --symbols TSLA \
  --days 5 \
  --table trading.features_1m_active
```

### AI Brain 如何调用

AI Brain 会根据 `tools_manifest.json` 自动理解工具用途。当看到 Gateway 错误时，AI 会:

1. **识别模式**: `invalid_features` → 数据完整性问题
2. **选择工具**: `diagnose_data_integrity`
3. **执行修复**: `auto_fix=True`
4. **验证结果**: 检查 coverage 是否恢复
5. **给出建议**: 根据 recommendation 字段回复用户

---

## 集成测试

### 测试 Layer A (开盘前检查)

```bash
# 1. 模拟覆盖率不足的情况 (可选，仅用于测试)
# (实际生产环境不需要此步)

# 2. 运行 morning_health.sh
./scripts/automation/morning_health.sh

# 预期输出:
# ================================================================
# Morning Health Check - Enhanced with Data Integrity
# ================================================================
# [System] Layer A: Pre-market Data Integrity Check & Auto-Seed
# [Check] Running features_1m_active sync check...
# [Success] ✅ Data Integrity Check Passed
#          Features Active Table is synced (≥98% coverage)
# ================================================================
```

### 测试 Layer B (实时监控)

```bash
# 1. 启动 Watchdog (后台)
nohup venv/bin/python tools/gateway_watchdog.py > logs/watchdog.log 2>&1 &

# 2. 模拟 Gateway 错误 (写入测试日志)
echo '{"decision":"blocked","block_reason":"[data_integrity] invalid_features: vwap","symbol":"TSLA"}' \
  >> runs/ai_trader/$(date +%Y-%m-%d)/ml_gateway_decisions.jsonl

# 3. 查看 Watchdog 是否触发修复
tail -f logs/watchdog.log
# 预期输出:
# [Watchdog] 🚨 Data Integrity Error Detected!
# [Watchdog] Symbol: TSLA
# [Watchdog] 🚑 Triggering Auto-Seed Repair...
# [Watchdog] ✅ Repair completed for TSLA

# 4. 停止 Watchdog
ps aux | grep gateway_watchdog
kill <PID>
```

### 测试 Layer C (AI 工具)

```bash
# 1. 测试诊断工具
venv/bin/python scripts/ai_trader/brain/tools_sql.py diagnose \
  --symbols TSLA \
  --date 2025-11-20 \
  --auto-fix

# 预期输出 (JSON):
# {
#   "ok": true,
#   "coverage_results": [{"symbol": "TSLA", "coverage": 1.0}],
#   "recommendation": "All symbols have ≥95% coverage. System is healthy."
# }

# 2. 测试特征覆盖工具
venv/bin/python scripts/ai_trader/brain/tools_sql.py coverage \
  --symbols TSLA \
  --days 5

# 预期输出 (JSON):
# {
#   "ok": true,
#   "air_columns": [],
#   "recommendation": "All features have ≥90% coverage. Feature quality is good."
# }
```

---

## 监控与告警

### Prometheus 指标 (TODO - 未来增强)

建议添加以下指标:
```python
# Layer A
morning_health_check_duration_seconds
morning_health_check_failures_total

# Layer B
watchdog_repairs_total{symbol="TSLA",result="success|failure"}
watchdog_cooldown_skips_total
watchdog_circuit_breaker_trips_total

# Layer C
ai_brain_diagnose_calls_total{tool="diagnose_data_integrity"}
ai_brain_tool_errors_total
```

### 当前日志监控

```bash
# 监控 Layer A
tail -f logs/morning_health.log | grep -E "(CRITICAL|Failed)"

# 监控 Layer B
tail -f logs/watchdog.log | grep -E "(repair_failed|circuit_breaker)"

# 监控 Gateway 错误
tail -f logs/ml_brain.log | grep "invalid_features"
```

---

## 故障排查

### 问题 1: Morning Health Check 总是失败

**症状**: `morning_health.sh` 退出码 1

**排查**:
```bash
# 1. 手动运行检查工具
venv/bin/python tools/check_active_completeness.py \
  --date 2025-11-20 \
  --symbols TSLA \
  --auto-seed-active

# 2. 检查 ClickHouse 连接
venv/bin/python -c "from libs.db.ch_pool import get_client; print(get_client().query('SELECT 1'))"

# 3. 检查 features_1m 是否有数据
venv/bin/python -c "
from libs.db.ch_pool import get_client
print(get_client().query(\"SELECT count() FROM trading.features_1m WHERE symbol='TSLA' AND toDate(ts) = '2025-11-20'\"))
"
```

**可能原因**:
- ClickHouse 未启动
- features_1m 表本身就没数据 (source 问题)
- bars_1m_ibkr 没有 bars (IBKR 未运行)

### 问题 2: Watchdog 不触发修复

**症状**: Gateway 报错但 Watchdog 无动作

**排查**:
```bash
# 1. 确认 Watchdog 正在运行
ps aux | grep gateway_watchdog

# 2. 检查日志文件路径是否正确
ls -lh runs/ai_trader/$(date +%Y-%m-%d)/ml_gateway_decisions.jsonl

# 3. 手动写入测试消息
echo '{"decision":"blocked","block_reason":"[data_integrity] invalid_features","symbol":"TEST"}' \
  >> runs/ai_trader/$(date +%Y-%m-%d)/ml_gateway_decisions.jsonl

# 4. 查看 Watchdog 日志
tail -20 logs/watchdog.log
```

**可能原因**:
- Watchdog 未启动
- 日志文件路径错误 (日期变更时)
- Cooldown 冷却中 (5分钟内已修复过)
- 熔断器触发 (1小时内修复超过 10 次)

### 问题 3: AI Brain 工具调用失败

**症状**: AI 回复 "Tool execution failed"

**排查**:
```bash
# 1. 手动测试工具
cd .
venv/bin/python scripts/ai_trader/brain/tools_sql.py diagnose \
  --symbols TSLA \
  --auto-fix

# 2. 检查工具脚本路径
ls -lh tools/check_active_completeness.py
ls -lh tools/check_feature_column_coverage.py

# 3. 查看 AI Brain 日志
tail -50 runs/ai_trader/$(date +%Y-%m-%d)/brain_tools/*.json
```

**可能原因**:
- 工具脚本不存在或权限不足
- ClickHouse 超时 (增加 timeout)
- JSON 解析错误 (工具输出格式改变)

---

## 性能优化建议

### Layer A 优化

**当前**: 检查所有 10 个 symbols
**优化**: 只检查今日活跃的 symbols

```bash
# 获取今日活跃 symbols (有订单/信号的)
ACTIVE_SYMBOLS=$(venv/bin/python -c "
from libs.db.ch_pool import get_client
result = get_client().query(\"SELECT DISTINCT symbol FROM shadow_orders WHERE date=today()\")
print(','.join([r['symbol'] for r in result]))
")

# 只检查活跃 symbols
venv/bin/python tools/check_active_completeness.py \
  --symbols "$ACTIVE_SYMBOLS" \
  --auto-seed-active
```

### Layer B 优化

**当前**: 监控所有 Gateway 决策
**优化**: 只监控 blocked 决策

```python
# 在 gateway_watchdog.py 中添加过滤
if decision.get("decision") != "blocked":
    continue  # 跳过 trade/hold 决策
```

### Layer C 优化

**当前**: AI 每次都调用真实工具
**优化**: 缓存最近 5 分钟的诊断结果

```python
# 在 tools_sql.py 中添加缓存
@functools.lru_cache(maxsize=10)
def diagnose_data_integrity_cached(symbols_tuple, date):
    return diagnose_data_integrity(list(symbols_tuple), date)
```

---

## 总结

三层防护体系实现了从 "被动响应" 到 "主动预防" 的质变:

**传统模式** (修复前):
```
盘中发现 → 手动排查 → 手动修复 → 重启系统 → 损失 30 分钟
```

**自动化模式** (修复后):
```
Layer A: 开盘前发现并修复 → 系统健康启动 (0 损失)
Layer B: 盘中发现并自愈 (1秒修复，用户无感知)
Layer C: AI 自主诊断根因 → 给出长期解决方案
```

**投资回报 (ROI)**:
- 开发时间: 4 小时
- 每次节省: 30 分钟人工排查 + 交易中断损失
- 预期收益: 每月节省 10+ 小时运维时间

**下一步**:
1. ✅ 已完成: 三层防护代码实施
2. ⏳ 待激活: 添加 crontab + 启动 Watchdog
3. ⏳ 待验证: 运行 1 周观察效果
4. ⏳ 待增强: 添加 Prometheus 监控指标

---

**维护者**: Claude (AI Assistant) + Trading Team
**最后更新**: 2025-11-25
**版本**: 1.0
