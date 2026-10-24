# ClickHouse 自动备份设置指南

## 概述

本文档介绍如何设置 ClickHouse 数据库的每日自动备份系统。

## 备份方案特点

- **备份时间**: 每天下午 4:01 (16:01)
- **备份位置**: `./backup/clickhouse/YYYY-MM-DD/`
- **保留策略**: 默认保留 30 天的备份
- **备份内容**:
  - 所有表的结构 (DDL)
  - 所有表的数据 (Native 格式)
  - 数据库统计信息
  - 自动生成的恢复脚本

## 快速开始

### 1. 手动测试备份

在设置自动化之前，先手动测试备份功能：

```bash
cd .
make backup-ch-daily
```

备份完成后，检查备份目录：

```bash
ls -lh backup/clickhouse/
ls -lh backup/clickhouse/$(date +%Y-%m-%d)/
```

你应该看到：
- `schemas/` - 表结构文件
- `data/` - 数据文件
- `stats.txt` - 统计信息
- `restore.sh` - 恢复脚本
- `tables.txt` - 表列表

### 2. 测试恢复功能

```bash
cd backup/clickhouse/$(date +%Y-%m-%d)/
bash restore.sh
```

## 自动化设置

### 方案 A: 使用 cron (推荐 - macOS/Linux)

#### 1. 编辑 crontab

```bash
crontab -e
```

#### 2. 添加以下行

```cron
# ClickHouse Daily Backup - Every day at 4:01 PM
1 16 * * * cd . && make backup-ch-daily >> ./backup/clickhouse/backup.log 2>&1
```

#### 3. 验证 cron 任务

```bash
crontab -l
```

#### 4. 查看备份日志

```bash
tail -f ./backup/clickhouse/backup.log
```

### 方案 B: 使用 launchd (macOS 推荐)

macOS 推荐使用 launchd 代替 cron，因为它更可靠。

#### 1. 创建 plist 文件

创建文件: `~/Library/LaunchAgents/com.trading.clickhouse.backup.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.trading.clickhouse.backup</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/make</string>
        <string>-C</string>
        <string>.</string>
        <string>backup-ch-daily</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>16</integer>
        <key>Minute</key>
        <integer>1</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>./backup/clickhouse/backup.log</string>

    <key>StandardErrorPath</key>
    <string>./backup/clickhouse/backup_error.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
    </dict>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

#### 2. 加载 launchd 任务

```bash
# 创建目录（如果不存在）
mkdir -p ~/Library/LaunchAgents

# 复制或创建 plist 文件到正确位置
# 然后加载任务
launchctl load ~/Library/LaunchAgents/com.trading.clickhouse.backup.plist
```

#### 3. 验证任务状态

```bash
launchctl list | grep trading
```

#### 4. 手动触发测试

```bash
launchctl start com.trading.clickhouse.backup
```

#### 5. 查看日志

```bash
tail -f ./backup/clickhouse/backup.log
```

#### 6. 停止或卸载任务（如需要）

```bash
# 临时停止
launchctl stop com.trading.clickhouse.backup

# 卸载（完全移除）
launchctl unload ~/Library/LaunchAgents/com.trading.clickhouse.backup.plist
```

## 快速设置脚本

为了方便，我们提供了一键设置脚本：

```bash
cd .
make setup-backup-cron
```

这将自动创建 launchd 配置并加载任务。

## 配置选项

### 修改备份时间

编辑 plist 文件中的 `StartCalendarInterval` 部分：

```xml
<key>StartCalendarInterval</key>
<dict>
    <key>Hour</key>
    <integer>16</integer>  <!-- 修改小时 (0-23) -->
    <key>Minute</key>
    <integer>1</integer>   <!-- 修改分钟 (0-59) -->
</dict>
```

重新加载：

```bash
launchctl unload ~/Library/LaunchAgents/com.trading.clickhouse.backup.plist
launchctl load ~/Library/LaunchAgents/com.trading.clickhouse.backup.plist
```

### 修改保留天数

在 `.env` 文件中添加：

```bash
BACKUP_RETENTION_DAYS=60  # 保留 60 天
```

或者在运行时指定：

```bash
BACKUP_RETENTION_DAYS=60 make backup-ch-daily
```

### 修改备份位置

在 `.env` 文件中添加：

```bash
BACKUP_DIR=/path/to/your/backup/directory
```

## 恢复备份

### 完整恢复

```bash
# 1. 进入备份目录
cd ./backup/clickhouse/2025-10-17/

# 2. 运行恢复脚本
bash restore.sh
```

### 手动恢复单个表

```bash
# 1. 进入备份目录
cd ./backup/clickhouse/2025-10-17/

# 2. 恢复表结构
clickhouse client < schemas/your_table_schema.sql

# 3. 恢复表数据
clickhouse client --query="INSERT INTO trading.your_table FORMAT Native" < data/your_table.native
```

## 监控和维护

### 检查备份历史

```bash
cat ./backup/clickhouse/backup_history.log
```

### 检查备份大小

```bash
du -sh ./backup/clickhouse/*
```

### 检查最近的备份

```bash
ls -lt ./backup/clickhouse/ | head -10
```

### 验证备份完整性

```bash
# 检查最新备份的统计信息
cat ./backup/clickhouse/$(ls -t ./backup/clickhouse/ | head -1)/stats.txt
```

## 故障排除

### 问题 1: 备份失败 - 无法连接 ClickHouse

**症状**: 错误信息 "ERROR: Cannot connect to ClickHouse"

**解决方案**:

```bash
# 检查 ClickHouse 是否运行
brew services list | grep clickhouse

# 如果未运行，启动 ClickHouse
brew services start clickhouse

# 测试连接
clickhouse client --query="SELECT 1"
```

### 问题 2: 备份脚本权限不足

**症状**: 错误信息 "Permission denied"

**解决方案**:

```bash
chmod +x ./scripts/backup/clickhouse_daily_backup.sh
```

### 问题 3: cron 任务未执行

**症状**: 备份未在预定时间执行

**解决方案**:

```bash
# 检查 cron 日志 (macOS)
log show --predicate 'eventMessage contains "cron"' --last 1h

# 或者使用 launchd
launchctl list | grep trading
launchctl start com.trading.clickhouse.backup  # 手动触发测试
```

### 问题 4: 磁盘空间不足

**症状**: 备份失败或系统变慢

**解决方案**:

```bash
# 检查磁盘空间
df -h

# 手动清理旧备份
find ./backup/clickhouse -type d -name "20*" -mtime +30 -exec rm -rf {} \;

# 或者调整保留天数
BACKUP_RETENTION_DAYS=7 make backup-ch-daily
```

### 问题 5: 恢复失败 - 表已存在

**症状**: 恢复时提示表已存在

**解决方案**:

```bash
# 选项 1: 先删除现有表
clickhouse client --query="DROP TABLE IF EXISTS trading.your_table"

# 选项 2: 恢复到不同的数据库
clickhouse client --query="CREATE DATABASE IF NOT EXISTS trading_restore"
# 修改 restore.sh 中的 CLICKHOUSE_DB=trading_restore
```

## 最佳实践

1. **定期测试恢复**: 每月至少测试一次恢复功能
2. **异地备份**: 考虑将备份复制到外部存储或云端
3. **监控备份大小**: 设置告警，当备份大小异常时通知
4. **验证备份**: 自动化验证备份文件完整性
5. **文档化**: 记录特殊情况和恢复步骤

## 高级配置

### 配置异地备份 (rsync)

创建文件 `scripts/backup/sync_to_remote.sh`:

```bash
#!/bin/bash
# 将备份同步到远程服务器

BACKUP_DIR="./backup/clickhouse"
REMOTE_USER="your_user"
REMOTE_HOST="backup.example.com"
REMOTE_PATH="/backups/clickhouse/"

rsync -avz --delete \
  "$BACKUP_DIR/" \
  "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH"
```

在 cron 或 launchd 中添加：

```cron
30 16 * * * ./scripts/backup/sync_to_remote.sh
```

### 配置 S3 备份

```bash
# 安装 AWS CLI
brew install awscli

# 配置 AWS 凭证
aws configure

# 创建 S3 同步脚本
cat > scripts/backup/sync_to_s3.sh <<'EOF'
#!/bin/bash
BACKUP_DIR="./backup/clickhouse"
S3_BUCKET="s3://your-bucket/clickhouse-backups/"

aws s3 sync "$BACKUP_DIR" "$S3_BUCKET" \
  --storage-class STANDARD_IA \
  --exclude "*.log"
EOF

chmod +x scripts/backup/sync_to_s3.sh
```

## 总结

完成以上设置后，你的 ClickHouse 数据库将每天下午 4:01 自动备份到 `./backup/clickhouse/`，并保留最近 30 天的备份。

如有任何问题，请查看日志文件：
- 备份日志: `./backup/clickhouse/backup.log`
- 错误日志: `./backup/clickhouse/backup_error.log`
- 备份历史: `./backup/clickhouse/backup_history.log`
