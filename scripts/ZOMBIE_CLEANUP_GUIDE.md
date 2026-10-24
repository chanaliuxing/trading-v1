# 僵尸会话自动清理指南

**Created**: 2025-11-01 20:30 EST
**Status**: ✅ Script Ready | ⚠️ Awaiting User Execution

---

## 🎯 **问题描述**

**当前状态** (2025-11-01 20:26):
- **203 个 RUNNING 会话**
- **165 个僵尸会话** (运行 > 24小时)
- 最老会话: **187.3 小时** (7.8 天) - 从 2025-10-25 开始

**影响**:
- 资源泄漏 (内存、连接池)
- 数据库膨胀
- 系统性能下降
- 统计数据不准确

---

## 📁 **创建的文件**

### 1. 主清理脚本
**文件**: `scripts/cleanup_zombie_sessions.py`
**功能**:
- 查找 RUNNING > 阈值的僵尸会话
- 更新状态为 TIMEOUT
- 记录清理日志
- 生成清理报告

### 2. Cron Job Wrapper
**文件**: `scripts/cleanup_zombie_sessions_cron.sh`
**功能**:
- 自动执行清理 (每6小时)
- 日志输出到 `logs/zombie_cleanup.log`
- 保持日志文件大小 (最近1000行)

### 3. 使用指南
**文件**: `scripts/ZOMBIE_CLEANUP_GUIDE.md` (本文档)

---

## 🚀 **使用方法**

### 方法 1: 手动运行 (推荐首次使用)

#### Step 1: 查看当前僵尸会话 (Dry Run)

```bash
cd .
./venv/bin/python scripts/cleanup_zombie_sessions.py --dry-run
```

**输出示例**:
```
================================================================================
🧹 僵尸会话清理器 v1.0
================================================================================
📊 阈值: > 24 小时
🔍 模式: 仅查看 (DRY RUN)
🕐 时间: 2025-11-01 20:26:46
================================================================================

⚠️  发现 165 个僵尸会话:

Session ID     Bot ID                    Mode    Hours Running   Started
-----------------------------------------------------------------------------------------------
68512de073e9   OPENAI_DEFAULT            PAPER   187.3           2025-10-25 01:11
afafba2d8416   DEEPSEEK_NVIDIA_DEFAULT   LIVE    122.6           2025-10-27 17:49
40bd63443885   DEEPSEEK_DEFAULT          LIVE    105.2           2025-10-28 11:17
...

ℹ️  DRY RUN 模式 - 不执行实际清理
```

---

#### Step 2: 执行清理 (真正清理)

```bash
# 清理 > 24小时的僵尸会话
./venv/bin/python scripts/cleanup_zombie_sessions.py --threshold 24

# 或者更保守 - 只清理 > 48小时的
./venv/bin/python scripts/cleanup_zombie_sessions.py --threshold 48
```

**输出示例**:
```
================================================================================
🧹 僵尸会话清理器 v1.0
================================================================================
📊 阈值: > 24 小时
🔍 模式: 执行清理
...

🧹 开始清理 165 个僵尸会话...

  [1/165] 清理 68512de073e9 (运行了 187.3h)... ✅
  [2/165] 清理 afafba2d8416 (运行了 122.6h)... ✅
  [3/165] 清理 40bd63443885 (运行了 105.2h)... ✅
  ...

================================================================================
📊 清理统计:
   发现僵尸会话: 165
   ✅ 成功清理:   165
   ❌ 失败:       0
================================================================================

✅ 清理完成！

💡 提示:
   - 清理日志已记录到: llm.session_cleanup_log
   - 查看日志: SELECT * FROM llm.session_cleanup_log ORDER BY ts_ny DESC LIMIT 10
   - 验证清理: SELECT count() FROM llm.llm_sessions WHERE status='TIMEOUT'
```

---

#### Step 3: 验证清理结果

```bash
# 查看清理统计
./venv/bin/python scripts/cleanup_zombie_sessions.py --stats
```

**预期结果**:
```
================================================================================
📊 会话统计
================================================================================

Status       Count      Avg Hours    Max Hours
--------------------------------------------------
CLOSED       371        44.8         188.7
TIMEOUT      165        87.5         187.3       <- 新增 TIMEOUT 状态
RUNNING      38         11.2         23.8        <- RUNNING 数量大幅下降
```

---

### 方法 2: 自动化运行 (生产环境推荐)

#### Step 1: 添加到 Crontab

```bash
# 编辑 crontab
crontab -e

# 添加以下行 (每6小时运行一次)
0 */6 * * * ./scripts/cleanup_zombie_sessions_cron.sh

# 或者每天午夜运行
0 0 * * * ./scripts/cleanup_zombie_sessions_cron.sh
```

**Cron 时间格式**:
```
分钟 小时 日 月 星期 命令

示例:
0 */6 * * *    # 每6小时 (0:00, 6:00, 12:00, 18:00)
0 */4 * * *    # 每4小时
0 0 * * *      # 每天午夜
0 2 * * *      # 每天凌晨2点
```

---

#### Step 2: 验证 Cron Job

```bash
# 查看当前 crontab
crontab -l

# 查看清理日志
tail -f logs/zombie_cleanup.log

# 手动测试 cron 脚本
./scripts/cleanup_zombie_sessions_cron.sh
```

---

## 📊 **查看清理日志**

### 方法 1: ClickHouse 日志表

```bash
docker exec -i trading_clickhouse clickhouse-client --query "
SELECT
    formatDateTime(ts_ny, '%Y-%m-%d %H:%M:%S') as time,
    substring(session_id, 1, 12) as session_id,
    action,
    details
FROM llm.session_cleanup_log
ORDER BY ts_ny DESC
LIMIT 20
FORMAT Pretty
"
```

---

### 方法 2: 文件日志

```bash
# 查看最近日志
tail -50 logs/zombie_cleanup.log

# 实时监控
tail -f logs/zombie_cleanup.log

# 搜索特定日期
grep "2025-11-01" logs/zombie_cleanup.log
```

---

## 🔧 **高级用法**

### 1. 自定义阈值

```bash
# 清理 > 12小时的会话 (更激进)
./venv/bin/python scripts/cleanup_zombie_sessions.py --threshold 12

# 清理 > 72小时的会话 (更保守)
./venv/bin/python scripts/cleanup_zombie_sessions.py --threshold 72
```

---

### 2. 连接到远程 ClickHouse

```bash
./venv/bin/python scripts/cleanup_zombie_sessions.py \
    --host 10.0.1.100 \
    --port 9000 \
    --threshold 24
```

---

### 3. 仅查看特定范围的僵尸会话

```bash
# 修改脚本或直接查询
docker exec -i trading_clickhouse clickhouse-client --query "
SELECT
    substring(session_id, 1, 12) as session_id,
    bot_id,
    mode,
    formatDateTime(start_ts_ny, '%Y-%m-%d %H:%M') as started,
    round((toUnixTimestamp(now()) - toUnixTimestamp(start_ts_ny)) / 3600, 1) as hours_running
FROM llm.llm_sessions
WHERE status = 'RUNNING'
  AND (toUnixTimestamp(now()) - toUnixTimestamp(start_ts_ny)) BETWEEN 24*3600 AND 48*3600
ORDER BY start_ts_ny ASC
FORMAT Pretty
"
```

---

## ⚠️ **注意事项**

### 1. 首次运行建议

✅ **推荐流程**:
1. 先运行 `--dry-run` 查看要清理的会话
2. 检查是否有重要会话 (如 LIVE mode)
3. 如有疑问，先用较高阈值 (如 48h)
4. 确认无误后再执行真正清理

❌ **不推荐**:
- 直接运行不看 dry-run 结果
- 阈值设置太低 (< 12h)
- 在交易时间执行大规模清理

---

### 2. 对 LIVE 模式会话的影响

**Q**: 清理会影响正在交易的 LIVE 会话吗?

**A**: 不会。脚本仅清理 **已经僵尸** 的会话 (> 阈值小时未结束)。正常运行的 LIVE 会话不会被清理。

**验证方法**:
```bash
# 查看当前 LIVE RUNNING 会话
docker exec -i trading_clickhouse clickhouse-client --query "
SELECT
    substring(session_id, 1, 12),
    mode,
    round((toUnixTimestamp(now()) - toUnixTimestamp(start_ts_ny)) / 3600, 1) as hours
FROM llm.llm_sessions
WHERE status = 'RUNNING' AND mode = 'LIVE'
ORDER BY start_ts_ny DESC
"
```

如果 hours < 24，不会被清理。

---

### 3. 清理操作不可逆

⚠️ **重要**: ALTER TABLE UPDATE 在 ClickHouse 中不可逆。

**但是**: 会话数据仍然存在，只是 status 改为 TIMEOUT，可以通过查询恢复统计。

**备份策略**:
```bash
# 清理前导出 RUNNING 会话列表
docker exec -i trading_clickhouse clickhouse-client --query "
SELECT * FROM llm.llm_sessions WHERE status='RUNNING'
FORMAT CSV
" > backup_running_sessions_$(date +%Y%m%d).csv
```

---

## 📈 **监控与告警**

### 1. Grafana Dashboard (已有)

**Dashboard 4**: http://localhost:3000/d/llm-sessions-monitor

**Panel**:
- **Zombie Sessions Counter** - 实时显示僵尸数量
- **Zombie Sessions Table** - 详细列表

---

### 2. Grafana Alert (已配置)

**Alert**: `zombie-sessions-high`

**触发条件**: zombie_count > 10

**For**: 15 minutes

**Action**: 告警发送到 Slack (如已配置)

---

### 3. 手动检查命令

```bash
# 快速检查僵尸会话数量
docker exec -i trading_clickhouse clickhouse-client --query "
SELECT count() as zombie_count
FROM llm.llm_sessions
WHERE status = 'RUNNING'
  AND (toUnixTimestamp(now()) - toUnixTimestamp(start_ts_ny)) > 86400
"

# 如果 > 10，考虑运行清理
```

---

## 🐛 **故障排查**

### Problem 1: 脚本报错 "clickhouse-driver 未安装"

**Solution**:
```bash
./venv/bin/pip install clickhouse-driver
```

---

### Problem 2: 清理后 Grafana 仍显示大量僵尸

**Cause**: ClickHouse 更新延迟 (ALTER TABLE UPDATE 是异步的)

**Solution**:
```bash
# 等待 1-2 分钟

# 或手动检查
docker exec -i trading_clickhouse clickhouse-client --query "
SELECT status, count() FROM llm.llm_sessions GROUP BY status
"
```

---

### Problem 3: Cron Job 未执行

**Check**:
```bash
# 1. 检查 crontab 是否存在
crontab -l

# 2. 检查 cron 服务状态 (macOS)
ps aux | grep cron

# 3. 检查日志
cat logs/zombie_cleanup.log

# 4. 手动测试 cron 脚本
./scripts/cleanup_zombie_sessions_cron.sh
```

---

### Problem 4: 清理失败 (ALTER TABLE 权限问题)

**Cause**: ClickHouse 默认用户没有 ALTER 权限

**Solution**:
```bash
# 检查权限
docker exec -i trading_clickhouse clickhouse-client --query "
SHOW GRANTS FOR default
"

# 如果没有 ALTER 权限，需要配置 ClickHouse 用户权限
# 或者联系管理员
```

---

## 📚 **参考命令速查**

```bash
# 1. 查看统计
./venv/bin/python scripts/cleanup_zombie_sessions.py --stats

# 2. Dry Run 查看僵尸
./venv/bin/python scripts/cleanup_zombie_sessions.py --dry-run

# 3. 执行清理 (24h阈值)
./venv/bin/python scripts/cleanup_zombie_sessions.py --threshold 24

# 4. 查看清理日志
docker exec -i trading_clickhouse clickhouse-client --query "
SELECT * FROM llm.session_cleanup_log ORDER BY ts_ny DESC LIMIT 10
FORMAT Pretty
"

# 5. 查看会话分布
docker exec -i trading_clickhouse clickhouse-client --query "
SELECT status, count() FROM llm.llm_sessions GROUP BY status
"

# 6. 添加 Cron Job
crontab -e
# 添加: 0 */6 * * * ./scripts/cleanup_zombie_sessions_cron.sh

# 7. 查看 Cron 日志
tail -f logs/zombie_cleanup.log
```

---

## 🎯 **推荐配置**

### 生产环境

**Cron 频率**: 每 6 小时
```
0 */6 * * * /path/to/cleanup_zombie_sessions_cron.sh
```

**阈值**: 24 小时 (默认)

**告警**: Grafana Alert 已配置 (> 10 个僵尸时告警)

---

### 开发/测试环境

**Cron 频率**: 每天 1 次
```
0 2 * * * /path/to/cleanup_zombie_sessions_cron.sh
```

**阈值**: 48 小时 (更宽松)

---

## ⏭️ **下一步**

### 立即行动 (推荐)

1. **首次清理** (立即):
   ```bash
   # Dry run 查看
   ./venv/bin/python scripts/cleanup_zombie_sessions.py --dry-run

   # 执行清理
   ./venv/bin/python scripts/cleanup_zombie_sessions.py --threshold 24
   ```

2. **配置自动化** (5分钟):
   ```bash
   crontab -e
   # 添加: 0 */6 * * * ./scripts/cleanup_zombie_sessions_cron.sh
   ```

3. **验证告警** (已完成):
   - Grafana Alert 已配置 ✅
   - Dashboard Panel 已添加 ✅

---

### 长期优化

1. **调查根因**: 为何会话未正常关闭?
   - 检查会话生命周期管理代码
   - 添加超时机制
   - 改进异常处理

2. **预防措施**:
   - 会话自动超时 (应用层)
   - 定期健康检查
   - 改进日志记录

3. **监控优化**:
   - 添加会话年龄分布图
   - 按 bot_id 分析僵尸会话模式
   - 建立会话清理基线

---

**Status**: ✅ Script Ready | ⚠️ Awaiting User Decision to Execute
**Next Action**: Run cleanup (dry-run first) → Configure cron → Investigate root cause
**Est. Cleanup Time**: 2-5 minutes (for 165 sessions)
