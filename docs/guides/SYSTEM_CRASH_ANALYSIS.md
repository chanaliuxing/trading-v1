# 系统Crash分析报告

**日期**: 2025-12-10
**分析时间**: 2025-12-10 11:30

---

## 问题概述

系统昨天(12月10日)crash,发现logs文件夹里有大量deepseek开头的空文件持续生成,即使crash后仍在运行。

---

## 根本原因分析

### 1. **权限问题导致LaunchAgent任务失败**

**错误信息**:
```
shell-init: error retrieving current directory: getcwd: cannot access parent directories: Operation not permitted
/bin/bash: ./scripts/automation/market_llm.sh: Operation not permitted
```

**影响范围**:
- `com.trading.market` (9:30 AM 启动)
- `com.trading.premarket` (7:30 AM 启动)
- `com.trading.morning.health` (7:00 AM 启动)
- `com.trading.health.hourly` (每小时启动)
- `com.trading.overnight`, `com.trading.evening.review`, `com.trading.ai_trader.learn`

**原因**:
LaunchAgent服务没有权限访问工作目录或执行脚本。这可能是由于:
- macOS Full Disk Access权限缺失
- Terminal.app 或 launchd 没有开发者工具权限
- 目录权限问题

---

### 2. **LLM Trader Bot 循环重启问题**

**证据**:

12月10日生成了**52个deepseek日志文件**,时间分布:
```
09:41:54 - 启动 (2个日志)
09:46:59 - 重启
09:52:00 - 重启
09:57:02 - 重启
10:02:03 - 重启
10:07:06 - 重启
... (每5分钟一次,持续到 11:22)
```

**日志内容**:
每个日志只有3行,然后立即退出:
```
2025-12-10 11:17:48 - LLMTraderBot - INFO - Logging initialized. Main log: logs/deepseek_20251210_111748.log
2025-12-10 11:17:48 - LLMTraderBot - INFO -                       API detailed log: logs/deepseek_20251210_111748_api_detailed.txt
2025-12-10 11:17:48 - LLMTraderBot - INFO - [V2.7] Sentiment analysis system enabled (3-dimensional)
```

**原因分析**:
1. `run_single_bot.py` 启动后立即crash
2. LaunchAgent配置没有 `KeepAlive` 或 `ThrottleInterval`,导致服务crash后立即重启
3. 循环重启导致日志文件疯狂生成

---

### 3. **run_all_providers.sh 启动多个进程**

**问题代码** (`llm_trader/run_all_providers.sh`):
脚本启动了4个并发的LLM交易机器人:
- OpenAI (60秒间隔)
- DeepSeek (65秒间隔)  ← **这是生成日志的来源**
- Gemini (70秒间隔)
- DeepSeek-NVIDIA (180秒间隔)

每个进程设置为运行10个周期(`--cycles 10`),如果crash会立即重启。

---

### 4. **API详细日志文件为空**

**现象**:
所有 `*_api_detailed.txt` 文件都是0字节。

**原因**:
Bot在初始化阶段就crash了,还没来得及调用任何LLM API,所以API日志文件是空的。

---

## 执行流程重建

```
09:30 AM - com.trading.market LaunchAgent 触发
    ↓
market_llm.sh 执行
    ↓
检测到 run_all_providers.sh 存在,执行它
    ↓
run_all_providers.sh 启动4个provider进程:
  - OpenAI (PID xxx)
  - DeepSeek (PID xxx) ← 问题进程
  - Gemini (PID xxx)
  - DeepSeek-NVIDIA (PID xxx)
    ↓
DeepSeek进程每次启动后crash (09:41, 09:46, 09:52, ...)
    ↓
run_all_providers.sh 的 wait 命令检测到进程退出
    ↓
可能有重启机制或循环逻辑导致持续重启
    ↓
生成52个日志文件 (从09:41到11:22)
```

---

## 具体问题定位

### 问题1: LaunchAgent权限错误

**文件**: `/workspace/Library/LaunchAgents/com.trading.market.plist`

**配置**:
```xml
<key>WorkingDirectory</key><string>.</string>
<key>ProgramArguments</key>
<array>
  <string>/bin/bash</string>
  <string>./scripts/automation/market_llm.sh</string>
</array>
```

**错误**:
```
Operation not permitted
```

**影响**:
所有定时任务失败,包括:
- 健康检查 (hourly_health.sh)
- 盘前交易 (premarket)
- 市场交易 (market)
- 盘后复盘 (evening_review)
- 隔夜交易 (overnight)

---

### 问题2: Bot启动即Crash

**文件**: `llm_trader/run_single_bot.py`

**症状**:
- 只输出3行日志就退出
- 没有异常堆栈信息
- 没有API调用

**可能原因**:
1. **依赖缺失**: 某个Python包在导入时失败
2. **配置错误**: `.env` 文件缺失关键配置
3. **API密钥无效**: DeepSeek API密钥过期或无效
4. **ClickHouse连接失败**: 数据库不可用
5. **异步事件循环问题**: `nest_asyncio.apply()` 失败

---

### 问题3: 循环重启机制

**可能来源**:
1. `run_all_providers.sh` 中的 `wait` 命令检测到进程退出后,可能有外层循环
2. LaunchAgent配置了自动重启(虽然plist里没看到 KeepAlive)
3. `market_llm.sh` 调用的 `run_all_providers.sh` 本身有重试逻辑

---

## 影响范围

### 受影响的服务

| 服务 | 状态 | 最后错误日志 | 影响 |
|------|------|-------------|------|
| com.trading.market | ❌ 失败 | market_error.log (09:30) | 9:30 AM市场交易未启动 |
| com.trading.premarket | ❌ 失败 | premarket_error.log (07:30) | 盘前交易未启动 |
| com.trading.morning.health | ❌ 失败 | morning_error.log (07:00) | 晨间健康检查失败 |
| com.trading.health.hourly | ❌ 失败 | health_cron_error.log (多次) | 小时级健康检查失败125次 |
| com.trading.overnight | ❌ 失败 | overnight_error.log (20:00) | 隔夜交易未启动 |
| com.trading.evening.review | ❌ 失败 | evening_review_error.log (16:30) | 盘后复盘未执行 |
| com.trading.ai_trader.learn | ❌ 失败 | ai_trader_learn_error.log (17:05) | AI学习任务失败 |
| com.trading.recon.daily | ✅ 正常 | - | 数据对账服务正常 |
| com.trading.test | ✅ 正常 | - | 测试服务正常 |

### 文件污染

- **52个deepseek日志文件** (12月10日)
- 大部分 `_api_detailed.txt` 文件为空
- 每个日志文件只有3行内容
- 占用少量磁盘空间(约35KB)

---

## 解决方案

### 立即修复

#### 1. 修复LaunchAgent权限问题

**步骤**:
```bash
# 1. 停止所有失败的服务
launchctl unload ~/Library/LaunchAgents/com.trading.market.plist
launchctl unload ~/Library/LaunchAgents/com.trading.premarket.plist
launchctl unload ~/Library/LaunchAgents/com.trading.health.hourly.plist
launchctl unload ~/Library/LaunchAgents/com.trading.morning.health.plist
launchctl unload ~/Library/LaunchAgents/com.trading.overnight.plist
launchctl unload ~/Library/LaunchAgents/com.trading.evening.review.plist
launchctl unload ~/Library/LaunchAgents/com.trading.ai_trader.learn.plist

# 2. 给予权限
# 打开: System Settings → Privacy & Security → Full Disk Access
# 添加: Terminal.app 和 /bin/bash

# 3. 清除扩展属性
xattr -cr ./scripts/automation/

# 4. 重新加载服务
launchctl load ~/Library/LaunchAgents/com.trading.market.plist
# ... (其他服务)
```

#### 2. 调试Bot Crash原因

**步骤**:
```bash
# 1. 手动运行bot,查看完整错误
cd ./llm_trader
source ../venv/bin/activate
python run_single_bot.py --provider deepseek --symbols TSLA --cycles 1

# 2. 检查依赖
pip list | grep -E "nest-asyncio|ib-insync"

# 3. 检查ClickHouse连接
clickhouse-client --host localhost --query "SELECT 1"

# 4. 检查.env文件
cat ./.env | grep DEEPSEEK_API_KEY

# 5. 测试provider直接调用
cd .
python -c "from llm_trader.providers.deepseek_provider import DeepSeekProvider; print('OK')"
```

#### 3. 清理日志文件

```bash
# 备份现有日志
mkdir -p logs/archive/crash_2025-12-10
mv logs/deepseek_20251210_* logs/archive/crash_2025-12-10/
mv logs/*_error.log logs/archive/crash_2025-12-10/

# 压缩备份
cd logs/archive
tar -czf crash_2025-12-10.tar.gz crash_2025-12-10/
rm -rf crash_2025-12-10/
```

#### 4. 修改LaunchAgent配置防止循环重启

**编辑**: `~/Library/LaunchAgents/com.trading.market.plist`

**添加节流配置**:
```xml
<key>ThrottleInterval</key>
<integer>300</integer>  <!-- 失败后至少等待5分钟 -->

<key>StartInterval</key>
<integer>3600</integer>  <!-- 如果没有Calendar配置,每小时最多运行一次 -->
```

---

### 长期改进

#### 1. 增强错误处理

**文件**: `llm_trader/run_single_bot.py`

**建议**:
```python
def main():
    try:
        # ... existing code ...
    except Exception as e:
        logger.error(f"FATAL ERROR: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)  # 明确退出码
```

#### 2. 添加健康检查

**创建**: `llm_trader/health_check.py`

```python
#!/usr/bin/env python3
"""Pre-flight health check before starting bots"""
import sys
import os

def check_health():
    # Check ClickHouse
    # Check API keys
    # Check dependencies
    # Check disk space
    return True

if __name__ == "__main__":
    if not check_health():
        sys.exit(1)
```

**修改**: `scripts/automation/market_llm.sh`

```bash
# 在启动bot前添加健康检查
cd "$PROJECT_ROOT/llm_trader"
python health_check.py || {
  echo "Health check failed. Abort." >&2
  exit 1
}
```

#### 3. 改进日志管理

**建议**:
- 使用日志轮转 (`logging.handlers.RotatingFileHandler`)
- 限制单个日志文件大小 (5MB)
- 保留最多10个历史文件
- 添加全局异常捕获

#### 4. 监控告警

**创建**: `scripts/monitoring/alert_on_crash.sh`

```bash
#!/bin/bash
# 检测重复crash并发送告警
CRASH_THRESHOLD=3
RECENT_CRASHES=$(find logs/ -name "deepseek_$(date +%Y%m%d)*" -mmin -10 | wc -l)

if [ $RECENT_CRASHES -gt $CRASH_THRESHOLD ]; then
  # 发送告警 (邮件/Slack/等)
  echo "ALERT: Detected $RECENT_CRASHES crashes in last 10 minutes"
fi
```

---

## 检查清单

- [ ] 修复LaunchAgent权限问题
- [ ] 手动运行bot找到crash原因
- [ ] 清理52个空日志文件
- [ ] 添加ThrottleInterval防止循环重启
- [ ] 增强错误处理和日志记录
- [ ] 添加pre-flight健康检查
- [ ] 设置crash监控告警
- [ ] 测试所有LaunchAgent服务
- [ ] 验证market_llm.sh正常工作
- [ ] 检查其他定时任务状态

---

## 附录

### 相关文件路径

```
./
├── llm_trader/
│   ├── run_single_bot.py           # Bot入口
│   ├── run_all_providers.sh        # 多provider并发脚本
│   └── llm_trader_bot.py           # Bot主逻辑
├── scripts/automation/
│   ├── market_llm.sh               # Market时段启动脚本
│   ├── common.sh                   # 共用环境设置
│   └── hourly_health.sh            # 小时健康检查
├── logs/
│   ├── market_error.log            # Market任务错误
│   ├── health_cron_error.log       # 健康检查错误(125KB)
│   └── deepseek_20251210_*.log     # 52个crash日志
└── ~/Library/LaunchAgents/
    ├── com.trading.market.plist
    ├── com.trading.premarket.plist
    └── com.trading.health.hourly.plist
```

### LaunchAgent服务状态

```bash
$ launchctl list | grep com.trading
-     126    com.trading.ai_trader.learn       # Exit code 126
-     126    com.trading.morning.health        # Exit code 126
-     126    com.trading.premarket             # Exit code 126
-     0      com.trading.recon.daily           # OK
-     126    com.trading.health.hourly         # Exit code 126
-     0      com.trading.test                  # OK
-     126    com.trading.evening.review        # Exit code 126
-     126    com.trading.overnight             # Exit code 126
-     126    com.trading.market                # Exit code 126
```

**Exit code 126**: "Permission denied" or "Command cannot execute"

---

## 结论

系统crash的根本原因是**LaunchAgent权限问题**,导致:
1. 所有定时任务失败 (exit code 126)
2. Bot启动脚本无法执行
3. 但Bot仍然被某种机制重复启动(可能是外层脚本重试或服务自动重启)
4. Bot每次启动后立即crash,生成日志文件但无实际内容
5. 循环持续约2小时,生成52个日志文件

**修复优先级**:
1. **立即**: 修复LaunchAgent权限,停止循环重启
2. **紧急**: 调试Bot crash原因(可能是依赖/配置/API问题)
3. **重要**: 添加节流配置防止重复crash
4. **改进**: 增强错误处理和监控告警

---

**生成时间**: 2025-12-10 11:30
**分析者**: Claude Code AI Assistant
