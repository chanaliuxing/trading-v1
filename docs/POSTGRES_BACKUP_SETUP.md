# PostgreSQL 完整备份文档

## 概述

PostgreSQL 备份方案采用企业级设计，确保：
- ✅ 每个数据库单独备份
- ✅ 结构（Schema）和数据（Data）分离
- ✅ 外键依赖顺序自动处理
- ✅ 全局对象（角色、表空间）单独备份
- ✅ 支持灵活的恢复选项

---

## 备份策略

### 备份内容

1. **全局对象** (`00_globals.sql.gz`)
   - 数据库角色 (roles)
   - 表空间 (tablespaces)
   - 其他全局配置

2. **每个数据库** (独立目录)
   - `<db>_schema.sql.gz` - 数据库结构
     - 表定义 (CREATE TABLE)
     - 索引 (CREATE INDEX)
     - 约束 (PRIMARY KEY, FOREIGN KEY, CHECK)
     - 视图、函数、触发器
   - `<db>_data.sql.gz` - 数据内容
     - 所有表的数据
     - 按外键依赖顺序导出
     - 使用 `--column-inserts` 确保兼容性
   - `<db>_full.sql.gz` - 完整备份（便利选项）
   - `restore.sh` - 自动恢复脚本

---

## 目录结构

```
./backup/pg/
├── 2025-10-18/
│   ├── 00_globals.sql.gz                # 全局对象
│   ├── test/                            # 数据库: test
│   │   ├── test_schema.sql.gz           # 结构
│   │   ├── test_data.sql.gz             # 数据
│   │   ├── test_full.sql.gz             # 完整备份
│   │   └── restore.sh                   # 恢复脚本
│   ├── mydb/                            # 数据库: mydb
│   │   ├── mydb_schema.sql.gz
│   │   ├── mydb_data.sql.gz
│   │   ├── mydb_full.sql.gz
│   │   └── restore.sh
│   └── BACKUP_MANIFEST.md               # 备份清单
├── backup.log                           # 备份日志
└── backup_history.log                   # 历史记录
```

---

## 快速开始

### 手动执行备份

```bash
cd .
make backup-pg-full
```

### 自动化已配置

PostgreSQL 备份已集成到每日自动化任务中：
- **时间**: 每天下午 4:01 PM
- **命令**: `make recon-all`（包含 PostgreSQL 和 ClickHouse 备份）

---

## 恢复方法

### 方法 1: 使用自动恢复脚本（推荐）

恢复单个数据库：
```bash
cd ./backup/pg/2025-10-18/test
bash restore.sh
```

### 方法 2: 手动恢复（完整备份）

```bash
# 1. 创建数据库
createdb -U postgres -h localhost test_restore

# 2. 恢复数据
gunzip -c test/test_full.sql.gz | psql -U postgres -h localhost -d test_restore
```

### 方法 3: 分步恢复（Schema + Data）

```bash
# 1. 创建数据库
createdb -U postgres -h localhost test_restore

# 2. 恢复结构
gunzip -c test/test_schema.sql.gz | psql -U postgres -h localhost -d test_restore

# 3. 恢复数据
gunzip -c test/test_data.sql.gz | psql -U postgres -h localhost -d test_restore
```

### 方法 4: 恢复所有数据库

```bash
cd ./backup/pg/2025-10-18

# 1. 恢复全局对象
gunzip -c 00_globals.sql.gz | psql -U postgres -h localhost

# 2. 恢复每个数据库
for db_dir in */; do
    if [ -d "$db_dir" ]; then
        cd "$db_dir"
        bash restore.sh
        cd ..
    fi
done
```

---

## 外键依赖处理

`pg_dump` 自动按照外键依赖顺序导出数据，确保：

1. **导出顺序**: 父表 → 子表
2. **导入顺序**: 相同顺序，保证外键约束不冲突
3. **禁用触发器**: 数据导入时使用 `--disable-triggers`

**示例**:
如果有表依赖关系: `orders` → `customers` (外键)
- 导出顺序: 先 `customers`，后 `orders`
- 导入时自动维持此顺序

---

## 配置说明

### 环境变量（从 .env 读取）

```bash
PG_HOST=localhost
PG_PORT=5432
PG_USER=postgres
PG_PASSWORD=postgres
```

### 自定义配置

编辑脚本 `scripts/backup/postgres_full_backup.sh` 的配置部分：

```bash
# === 1. Configuration ===
PG_USER="${PG_USER:-postgres}"
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
BACKUP_ROOT="${BACKUP_DIR:-$PROJECT_ROOT/backup/pg}"
```

---

## 备份选项详解

### Schema Only (`--schema-only`)

导出内容:
- `CREATE TABLE` 语句
- `CREATE INDEX` 语句
- 主键、外键、唯一约束
- 视图、函数、触发器定义
- 序列 (sequences)

**不包含**: 表数据

### Data Only (`--data-only`)

导出内容:
- 所有表的 `INSERT` 语句
- 按外键依赖顺序排列

选项:
- `--column-inserts`: 使用列名插入（兼容性好）
- `--disable-triggers`: 导入时禁用触发器

**不包含**: 表结构

### Full Backup

完整导出，包含 schema + data，适合快速恢复。

---

## 管理命令

### 查看备份列表

```bash
ls -lh ./backup/pg/
```

### 查看备份大小

```bash
du -sh ./backup/pg/*
```

### 查看备份清单

```bash
cat ./backup/pg/2025-10-18/BACKUP_MANIFEST.md
```

### 查看备份日志

```bash
tail -50 ./backup/pg/backup.log
```

### 查看备份历史

```bash
cat ./backup/pg/backup_history.log
```

---

## 故障排除

### 问题 1: psql 命令未找到

**症状**: `command not found: psql`

**解决方案**:
```bash
# macOS
brew install postgresql@14

# 或添加到 PATH
export PATH="/opt/homebrew/opt/postgresql@14/bin:$PATH"
```

### 问题 2: 连接失败

**症状**: `could not connect to server`

**解决方案**:
```bash
# 检查 PostgreSQL 是否运行
pg_isready -h localhost -p 5432

# 启动 PostgreSQL (如果使用 Homebrew)
brew services start postgresql@14

# 或检查 Docker 容器
docker ps | grep postgres
```

### 问题 3: 权限错误

**症状**: `permission denied`

**解决方案**:
```bash
# 确保用户有权限
psql -U postgres -c "SELECT current_user, session_user;"

# 如果需要，使用超级用户
export PG_USER=postgres
```

### 问题 4: 恢复时外键冲突

**症状**: `violates foreign key constraint`

**解决方案**:

这不应该发生，因为 `pg_dump` 已按依赖顺序导出。如果仍然出现：

```bash
# 方法 1: 使用 --disable-triggers
psql -U postgres -d mydb <<EOF
SET session_replication_role = replica;
\i data.sql
SET session_replication_role = DEFAULT;
EOF

# 方法 2: 先导入 schema，再导入 data
gunzip -c mydb_schema.sql.gz | psql -U postgres -d mydb
gunzip -c mydb_data.sql.gz | psql -U postgres -d mydb
```

---

## 性能优化

### 加速备份

```bash
# 使用自定义格式（更快，但不可读）
pg_dump -U postgres -F c -f backup.dump mydb

# 并行备份（PostgreSQL 9.3+）
pg_dump -U postgres -j 4 -F d -f backup_dir/ mydb
```

### 加速恢复

```bash
# 使用自定义格式恢复
pg_restore -U postgres -d mydb -j 4 backup.dump

# 禁用 fsync（仅测试环境）
psql -U postgres -c "ALTER SYSTEM SET fsync = off;"
# 恢复后记得重新启用！
```

---

## 最佳实践

1. **定期测试恢复** - 每月至少测试一次恢复流程
2. **异地备份** - 将备份复制到远程位置
3. **保留策略** - 建议保留至少 30 天的备份
4. **加密备份** - 对于敏感数据，考虑加密备份文件
5. **验证备份** - 检查备份文件大小和完整性

---

## 高级用法

### 备份特定表

```bash
pg_dump -U postgres -t 'schema.table_name' -f table_backup.sql mydb
```

### 排除特定表

```bash
pg_dump -U postgres -T 'schema.large_table' -f backup.sql mydb
```

### 仅备份数据（不含大对象）

```bash
pg_dump -U postgres --data-only --no-blobs -f data_only.sql mydb
```

### 备份到远程主机

```bash
pg_dump -U postgres -h remote.host.com -p 5432 mydb | gzip > remote_backup.sql.gz
```

---

## 相关文档

- **每日自动化文档**: `docs/DAILY_AUTOMATION_SETUP.md`
- **ClickHouse 备份文档**: `docs/CLICKHOUSE_BACKUP_SETUP.md`
- **PostgreSQL 官方文档**: https://www.postgresql.org/docs/current/backup.html

---

## 安全注意事项

1. **密码保护**: 不要在脚本中硬编码密码
   ```bash
   # 使用 .pgpass 文件
   echo "localhost:5432:*:postgres:password" > ~/.pgpass
   chmod 600 ~/.pgpass
   ```

2. **备份加密**: 敏感数据建议加密
   ```bash
   pg_dump mydb | gzip | gpg -e -r your@email.com > backup.sql.gz.gpg
   ```

3. **访问控制**: 限制备份目录权限
   ```bash
   chmod 700 ./backup/pg
   ```

---

**最后更新**: 2025-10-18
**维护者**: Trading Team
**下次审查**: 每月第一个周一
