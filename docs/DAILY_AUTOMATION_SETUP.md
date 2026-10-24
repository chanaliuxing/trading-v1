# 每日自动化任务设置文档

## 概述

系统已配置为每天下午 4:01 PM 自动运行完整的数据处理流程，包括：

1. **数据协调** (Data Reconciliation) - 所有时间周期
2. **特征计算** (Feature Computation) - 所有分辨率
3. **ClickHouse 完整备份** - 所有表结构和数据

---

## ✅ 已配置的自动化任务

### 时间表
- **执行时间**: 每天下午 4:01 PM (16:01)
- **任务**: `make recon-all`

### 任务内容

#### 1. 数据协调 (Reconciliation)
对以下时间周期进行数据协调：
- 1 分钟 (1m)
- 5 分钟 (5m)
- 10 分钟 (10m)
- 15 分钟 (15m)
- 30 分钟 (30m)
- 2 小时 (2h)
- 3 小时 (3h)
- 4 小时 (4h)
- 1 天 (1d)

#### 2. 特征计算
计算所有分辨率的特征，回溯 3 天数据。

#### 3. ClickHouse 备份
- **备份位置**: `./backup/clickhouse/YYYY-MM-DD/`
- **备份内容**:
  - 所有 61 个表的结构 (DDL)
  - 所有有数据的表的数据 (Native 格式)
  - 统计信息和恢复脚本
- **保留策略**: 永久保留，需手动清理

---

## 📁 相关文件

### 配置文件
- **Launchd Plist**: `~/Library/LaunchAgents/com.trading.recon.daily.plist`
- **设置脚本**: `scripts/setup_daily_automation.sh`
- **Makefile 命令**: `make setup-daily-automation`

### 日志文件
- **标准输出**: `logs/recon_daily.log`
- **错误输出**: `logs/recon_daily_error.log`
- **备份历史**: `backup/clickhouse/backup_history.log`

---

## 🚀 管理命令

### 查看状态
```bash
# 检查 launchd 任务状态
launchctl list | grep trading

# 查看任务详情
launchctl list com.trading.recon.daily
```

### 手动触发
```bash
# 方式 1: 直接运行命令 (推荐测试用)
make recon-all

# 方式 2: 通过 launchd 触发
launchctl start com.trading.recon.daily
```

### 查看日志
```bash
# 实时查看输出日志
tail -f logs/recon_daily.log

# 实时查看错误日志
tail -f logs/recon_daily_error.log

# 查看备份历史
cat backup/clickhouse/backup_history.log

# 查看最近一次运行的备份验证报告
cat backup/clickhouse/$(ls -t backup/clickhouse | head -1)/VERIFICATION_REPORT.md
```

### 停止/重启任务
```bash
# 停止自动化任务
launchctl unload ~/Library/LaunchAgents/com.trading.recon.daily.plist

# 重新加载任务
launchctl load ~/Library/LaunchAgents/com.trading.recon.daily.plist

# 重启任务 (先停止再启动)
launchctl unload ~/Library/LaunchAgents/com.trading.recon.daily.plist && \
launchctl load ~/Library/LaunchAgents/com.trading.recon.daily.plist
```

---

## 🔧 自定义配置

### 修改执行时间

编辑 plist 文件:
```bash
vim ~/Library/LaunchAgents/com.trading.recon.daily.plist
```

修改 `StartCalendarInterval` 部分:
```xml
<key>StartCalendarInterval</key>
<dict>
    <key>Hour</key>
    <integer>16</integer>  <!-- 修改小时 (0-23) -->
    <key>Minute</key>
    <integer>1</integer>   <!-- 修改分钟 (0-59) -->
</dict>
```

重新加载:
```bash
launchctl unload ~/Library/LaunchAgents/com.trading.recon.daily.plist
launchctl load ~/Library/LaunchAgents/com.trading.recon.daily.plist
```

### 修改任务内容

编辑 Makefile 中的 `recon-all` 目标（第 188-207 行）。

---

## 📊 执行流程

每天下午 4:01 PM，系统会按以下顺序执行：

```
16:01:00 - 开始执行 recon-all
├── 16:01:00 - 数据协调 (1m bars)
├── 16:02:30 - 数据协调 (5m bars)
├── 16:04:00 - 数据协调 (10m bars)
├── 16:05:30 - 数据协调 (15m bars)
├── 16:07:00 - 数据协调 (30m bars)
├── 16:08:30 - 数据协调 (2h bars)
├── 16:10:00 - 数据协调 (3h bars)
├── 16:11:30 - 数据协调 (4h bars)
├── 16:13:00 - 数据协调 (1d bars)
├── 16:15:00 - 特征计算 (所有分辨率, 3天数据)
└── 16:30:00 - ClickHouse 完整备份
    └── 16:35:00 - 备份完成 (~1.6 GB)
```

**预计总耗时**: 约 30-35 分钟（取决于数据量）

---

## 🔍 监控和告警

### 检查最近一次运行

```bash
# 查看最后 50 行日志
tail -50 logs/recon_daily.log

# 查看是否有错误
tail -50 logs/recon_daily_error.log

# 检查今天的备份
ls -lh backup/clickhouse/$(date +%Y-%m-%d)/
```

### 验证备份完整性

```bash
# 查看今天的备份验证报告
cat backup/clickhouse/$(date +%Y-%m-%d)/VERIFICATION_REPORT.md
```

### 告警条件

需要人工介入的情况：
- ❌ 错误日志文件不为空
- ❌ 备份目录不存在或为空
- ❌ 备份验证报告显示数据不一致
- ❌ 日志文件显示进程被中断

---

## 🛠️ 故障排除

### 问题 1: 任务没有执行

**症状**: 日志文件没有更新，或者 4:01 PM 后没有新的备份

**解决方案**:
```bash
# 1. 检查任务是否加载
launchctl list | grep trading

# 2. 如果没有，重新加载
launchctl load ~/Library/LaunchAgents/com.trading.recon.daily.plist

# 3. 检查 plist 文件是否存在
ls -l ~/Library/LaunchAgents/com.trading.recon.daily.plist

# 4. 手动触发测试
launchctl start com.trading.recon.daily
```

### 问题 2: 执行失败或出错

**症状**: 错误日志中有异常信息

**解决方案**:
```bash
# 1. 查看错误详情
cat logs/recon_daily_error.log

# 2. 手动运行测试
make recon-all

# 3. 检查依赖服务
docker ps | grep clickhouse
docker ps | grep kafka

# 4. 检查磁盘空间
df -h
```

### 问题 3: 备份失败

**症状**: 备份目录不完整或没有创建

**解决方案**:
```bash
# 1. 检查 ClickHouse 是否运行
docker ps | grep clickhouse

# 2. 手动执行备份测试
make backup-ch-daily

# 3. 查看备份日志
cat backup/clickhouse/backup.log

# 4. 检查磁盘空间
du -sh backup/clickhouse/
```

### 问题 4: 数据协调失败

**症状**: 某些时间周期的数据协调报错

**解决方案**:
```bash
# 1. 单独测试失败的分辨率
make recon1m  # 测试 1m bars

# 2. 检查 Python 环境
python3 -m services.reconciliation.offline_data_reconciler --help

# 3. 查看详细错误
tail -100 logs/recon_daily_error.log
```

---

## 📚 相关文档

- **ClickHouse 备份详细文档**: `docs/CLICKHOUSE_BACKUP_SETUP.md`
- **架构文档**: `README_ARCHITECTURE.md`
- **代码库分析**: `CODEBASE_ANALYSIS.md`

---

## 🔄 手动清理旧备份

备份会永久保留，需要手动清理：

```bash
# 查看所有备份
ls -lh backup/clickhouse/

# 删除 60 天前的备份
find backup/clickhouse -type d -name '20*' -mtime +60 -exec rm -rf {} \;

# 删除指定日期的备份
rm -rf backup/clickhouse/2025-10-01

# 查看备份占用空间
du -sh backup/clickhouse/*
```

---

## ✅ 初始设置完成清单

- [x] Launchd 任务已创建并加载
- [x] 日志目录已创建
- [x] 备份目录已创建
- [x] 首次备份已验证
- [x] Makefile 命令已更新
- [x] 文档已创建

---

## 📞 支持

如有问题，请检查：
1. 日志文件: `logs/recon_daily.log` 和 `logs/recon_daily_error.log`
2. 备份验证报告: `backup/clickhouse/YYYY-MM-DD/VERIFICATION_REPORT.md`
3. Launchd 任务状态: `launchctl list | grep trading`

---

**最后更新**: 2025-10-18
**维护者**: Trading Team
**下次审查**: 每月第一个周一
