# 完整备份系统概览

## 🎯 总体架构

交易系统配置了完整的企业级备份方案，每天自动备份 PostgreSQL 和 ClickHouse 两个数据库。

---

## ⏰ 自动化时间表

**每天下午 4:01 PM，系统自动执行以下任务：**

```
16:01 - 开始执行 (make recon-all)
├── 16:01-16:15 - 数据协调 (9 个时间周期)
├── 16:15-16:30 - 特征计算 (所有分辨率)
├── 16:30-16:35 - PostgreSQL 完整备份
│   ├── 全局对象 (roles, tablespaces)
│   └── 每个数据库
│       ├── Schema (结构)
│       ├── Data (数据)
│       └── Full (完整备份)
└── 16:35-16:40 - ClickHouse 完整备份
    ├── 61 个表结构
    ├── 29 个有数据的表
    └── 验证报告

预计总耗时: 35-40 分钟
完成时间: ~16:40
```

---

## 📊 两个数据库备份对比

| 特性 | PostgreSQL | ClickHouse |
|------|-----------|------------|
| **备份位置** | `backup/pg/YYYY-MM-DD/` | `backup/clickhouse/YYYY-MM-DD/` |
| **备份方式** | 每个数据库单独备份 | 所有表统一备份 |
| **结构/数据** | 分离备份（3个文件） | 分离备份（2个目录） |
| **压缩** | gzip 自动压缩 | Native 格式（未压缩） |
| **外键处理** | pg_dump 自动排序 | 无外键，按表名排序 |
| **恢复脚本** | 每个数据库独立脚本 | 统一恢复脚本 |
| **全局对象** | 单独备份 (00_globals.sql.gz) | 不适用 |
| **验证报告** | BACKUP_MANIFEST.md | VERIFICATION_REPORT.md |

---

## 📁 完整目录结构

```
./backup/
├── pg/                                    # PostgreSQL 备份
│   ├── 2025-10-18/
│   │   ├── 00_globals.sql.gz              # 全局对象
│   │   ├── test/                          # 数据库: test
│   │   │   ├── test_schema.sql.gz         # 结构
│   │   │   ├── test_data.sql.gz           # 数据
│   │   │   ├── test_full.sql.gz           # 完整备份
│   │   │   └── restore.sh                 # 恢复脚本
│   │   └── BACKUP_MANIFEST.md             # 备份清单
│   ├── backup.log                         # 备份日志
│   └── backup_history.log                 # 历史记录
│
└── clickhouse/                            # ClickHouse 备份
    ├── 2025-10-18/
    │   ├── schemas/                       # 表结构 (61 个文件)
    │   │   ├── bars_1m_schema.sql
    │   │   └── ...
    │   ├── data/                          # 表数据 (29 个文件)
    │   │   ├── bars_1m.native
    │   │   └── ...
    │   ├── tables.txt                     # 表列表
    │   ├── stats.txt                      # 统计信息
    │   ├── restore.sh                     # 恢复脚本
    │   └── VERIFICATION_REPORT.md         # 验证报告
    ├── backup.log                         # 备份日志
    └── backup_history.log                 # 历史记录
```

---

## 🚀 快速命令

### 手动执行备份

```bash
# PostgreSQL 完整备份
make backup-pg-full

# ClickHouse 完整备份
make backup-ch-daily

# 两者都备份
make recon-all
```

### 查看备份

```bash
# PostgreSQL 备份列表
ls -lh backup/pg/

# ClickHouse 备份列表
ls -lh backup/clickhouse/

# 查看今天的备份
ls -lh backup/pg/$(date +%Y-%m-%d)/
ls -lh backup/clickhouse/$(date +%Y-%m-%d)/
```

### 恢复备份

**PostgreSQL**:
```bash
cd backup/pg/2025-10-18/test
bash restore.sh
```

**ClickHouse**:
```bash
cd backup/clickhouse/2025-10-18
bash restore.sh
```

---

## 📝 日志文件

### 自动化任务日志

```bash
# 每日任务输出
tail -f logs/recon_daily.log

# 每日任务错误
tail -f logs/recon_daily_error.log
```

### 备份专用日志

```bash
# PostgreSQL 备份日志
tail -50 backup/pg/backup.log
cat backup/pg/backup_history.log

# ClickHouse 备份日志
cat backup/clickhouse/backup_history.log
```

---

## 🔍 验证备份完整性

### PostgreSQL

```bash
# 查看备份清单
cat backup/pg/$(date +%Y-%m-%d)/BACKUP_MANIFEST.md

# 检查备份大小
du -sh backup/pg/$(date +%Y-%m-%d)/*
```

### ClickHouse

```bash
# 查看验证报告
cat backup/clickhouse/$(date +%Y-%m-%d)/VERIFICATION_REPORT.md

# 检查表数量
echo "Schemas: $(ls -1 backup/clickhouse/$(date +%Y-%m-%d)/schemas/ | wc -l)"
echo "Data files: $(ls -1 backup/clickhouse/$(date +%Y-%m-%d)/data/ | wc -l)"
```

---

## ⚙️ 管理命令

### 自动化任务管理

```bash
# 查看任务状态
launchctl list | grep trading

# 手动触发任务
launchctl start com.trading.recon.daily

# 停止自动化
launchctl unload ~/Library/LaunchAgents/com.trading.recon.daily.plist

# 重新加载
launchctl load ~/Library/LaunchAgents/com.trading.recon.daily.plist
```

### 备份管理

```bash
# 查看所有备份大小
du -sh backup/pg/* backup/clickhouse/*

# 删除旧备份 (例如 60 天前)
find backup/pg -type d -name '20*' -mtime +60 -exec rm -rf {} \;
find backup/clickhouse -type d -name '20*' -mtime +60 -exec rm -rf {} \;

# 统计备份总大小
du -sh backup/
```

---

## 🛠️ 故障排除

### PostgreSQL 备份失败

```bash
# 检查 PostgreSQL 连接
psql -U postgres -h localhost -p 5432 -l

# 检查 pg_dump 是否安装
which pg_dump || echo "需要安装: brew install postgresql@14"

# 手动测试备份
make backup-pg-full
```

### ClickHouse 备份失败

```bash
# 检查 ClickHouse 状态
docker ps | grep clickhouse

# 测试连接
docker exec -i trading_clickhouse clickhouse-client --query="SELECT 1"

# 手动测试备份
make backup-ch-daily
```

### 自动化任务未执行

```bash
# 检查 launchd 任务
launchctl list | grep trading

# 查看日志
tail -100 logs/recon_daily_error.log

# 手动触发测试
make recon-all
```

---

## 📚 详细文档

- **PostgreSQL 备份详解**: `docs/POSTGRES_BACKUP_SETUP.md`
- **ClickHouse 备份详解**: `docs/CLICKHOUSE_BACKUP_SETUP.md`
- **每日自动化说明**: `docs/DAILY_AUTOMATION_SETUP.md`

---

## ✅ 设置检查清单

- [x] PostgreSQL 备份脚本创建
- [x] ClickHouse 备份脚本创建
- [x] Makefile 命令添加
- [x] 自动化任务配置 (launchd)
- [x] 日志目录创建
- [x] 备份目录创建
- [x] 文档完善

---

## 🔐 安全最佳实践

1. **密码管理**
   - PostgreSQL: 使用 `.pgpass` 文件
   - 不要在脚本中硬编码密码

2. **访问控制**
   ```bash
   chmod 700 backup/pg backup/clickhouse
   ```

3. **备份加密** (可选)
   ```bash
   # 加密备份
   tar czf - backup/pg/2025-10-18 | gpg -e -r your@email.com > backup.tar.gz.gpg
   ```

4. **异地备份** (推荐)
   ```bash
   # 复制到远程服务器
   rsync -avz backup/ user@remote:/backups/trading/

   # 或上传到 S3
   aws s3 sync backup/ s3://your-bucket/trading-backups/
   ```

---

## 📈 监控建议

### 每天检查

1. 查看备份是否成功
   ```bash
   ls -lh backup/pg/$(date +%Y-%m-%d)
   ls -lh backup/clickhouse/$(date +%Y-%m-%d)
   ```

2. 检查备份大小是否正常
   ```bash
   du -sh backup/*/$(date +%Y-%m-%d)
   ```

3. 查看日志是否有错误
   ```bash
   tail -20 logs/recon_daily_error.log
   ```

### 每周检查

1. 测试恢复流程
2. 验证备份完整性
3. 清理旧备份

### 每月检查

1. 完整恢复测试
2. 审查备份策略
3. 更新文档

---

## 🎯 恢复演练

建议每月进行一次完整恢复演练：

```bash
# 1. 创建测试环境
createdb -U postgres test_restore_pg
docker exec trading_clickhouse clickhouse-client --query="CREATE DATABASE test_restore_ch"

# 2. 恢复 PostgreSQL
cd backup/pg/$(date +%Y-%m-%d)/test
bash restore.sh

# 3. 恢复 ClickHouse
cd backup/clickhouse/$(date +%Y-%m-%d)
bash restore.sh

# 4. 验证数据
psql -U postgres -d test_restore_pg -c "\dt"
docker exec trading_clickhouse clickhouse-client --database=test_restore_ch --query="SHOW TABLES"

# 5. 清理测试环境
dropdb -U postgres test_restore_pg
docker exec trading_clickhouse clickhouse-client --query="DROP DATABASE test_restore_ch"
```

---

**最后更新**: 2025-10-18
**维护者**: Trading Team
**紧急联系**: 查看各备份日志文件
