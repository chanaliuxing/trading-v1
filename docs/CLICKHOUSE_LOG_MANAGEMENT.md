# ClickHouse 日志管理配置指南

## 概述

本文档说明 ClickHouse 的日志管理配置，旨在解决日志快速增长导致磁盘空间耗尽的问题。

## 实施的优化措施

### 1. 数据存储优化

**问题**: Docker Desktop 虚拟磁盘空间有限（默认 ~60GB）

**解决方案**: 将 ClickHouse 数据和日志存储到宿主机目录

```yaml
# infra/compose.yml
volumes:
  - /workspace/Documents/clickhouse_data:/var/lib/clickhouse
  - /workspace/Documents/clickhouse_logs:/var/log/clickhouse-server
```

**收益**:
- 数据存储在宿主机 200GB+ 空间
- 避免 Docker 虚拟磁盘限制
- 便于直接访问和备份

### 2. 禁用最话痨的系统日志

**配置文件**: `infra/clickhouse-config/10-logs.xml`

**禁用的日志表**:
- `processors_profile_log` - 处理器性能分析日志（增长最快）
- `trace_log` - 详细执行跟踪日志（数据量极大）

**保留的日志表**:
- `query_log` - 查询日志（带采样，5%）
- `query_thread_log` - 查询线程日志（TTL 3天）
- `text_log` - 文本日志（仅 ERROR 级别，TTL 3天）
- `metric_log` - 指标日志（TTL 7天）
- `part_log` - 分区日志（TTL 3天）

### 3. 日志级别降低

**配置**: 服务器日志级别设置为 `error`

```xml
<logger>
    <level>error</level>
    <console>true</console>
</logger>
```

**收益**:
- 减少 `text_log` 表的写入量
- 仅记录严重错误，避免噪音

### 4. 查询日志采样

**配置文件**: `infra/clickhouse-config/10-default-profile.xml`

```xml
<profiles>
    <default>
        <log_queries>1</log_queries>
        <log_queries_probability>0.05</log_queries_probability>  <!-- 5% 采样 -->
    </default>
</profiles>
```

**收益**:
- 仅记录 5% 的查询日志
- 保留诊断能力的同时大幅减少日志量
- 可根据需要调整采样率（10%, 20% 等）

### 5. TTL 自动清理

**TTL 配置**:
- 高频日志: 3 天 TTL (`text_log`, `query_thread_log`, `part_log`)
- 指标日志: 7 天 TTL (`query_log`, `metric_log`, `asynchronous_metric_log`)

**自动执行**: ClickHouse 后台自动清理过期数据

### 6. Docker 容器日志轮转

**配置**:
```yaml
logging:
  driver: local
  options:
    max-size: "100m"   # 单个日志文件最大 100MB
    max-file: "10"      # 保留最多 10 个文件
```

**收益**:
- 容器 stdout 日志不会无限增长
- 总共约 1GB 日志空间
- 自动轮转，无需手动清理

## 维护脚本使用

### 快速检查

```bash
cd .
./scripts/clickhouse-maintenance.sh check
```

**输出**:
- 磁盘使用情况
- 日志表体量排名
- 宿主机目录大小

### 每周巡检（推荐）

```bash
./scripts/clickhouse-maintenance.sh weekly
```

**功能**:
- 检查 TTL 配置状态
- 查看日志表体量和最老数据
- 触发 OPTIMIZE 清理过期数据
- 显示磁盘使用情况

**建议**: 添加到 crontab，每周自动执行

```bash
# 每周日凌晨 2 点执行巡检
0 2 * * 0 cd . && ./scripts/clickhouse-maintenance.sh weekly >> /var/log/clickhouse-weekly-clean.log 2>&1
```

### 手动触发清理

```bash
./scripts/clickhouse-maintenance.sh optimize
```

**用途**: 立即触发 OPTIMIZE 清理过期数据（不等待自动清理）

### 紧急清空日志（磁盘告急时）

```bash
./scripts/clickhouse-maintenance.sh emergency
```

**警告**: ⚠️ 会删除所有系统日志数据，仅在磁盘严重不足时使用！

## 目录结构

```
/workspace/Documents/
├── clickhouse_data/          # ClickHouse 数据目录（6.8GB，会持续增长）
└── clickhouse_logs/          # ClickHouse 服务日志（72KB，轮转控制）

./
├── infra/
│   ├── compose.yml                       # Docker Compose 配置
│   ├── clickhouse-config/
│   │   ├── 10-logs.xml                  # 日志优化配置
│   │   ├── 10-default-profile.xml       # 查询日志采样配置
│   │   └── backup-config.xml            # 备份配置
│   └── clickhouse-users.d/
│       └── default-user.xml             # 用户配置
└── scripts/
    ├── clickhouse-init-logs.sql         # 初始化 TTL（一次性）
    ├── clickhouse-weekly-clean.sql      # 每周巡检 SQL
    ├── clickhouse-emergency-clean.sql   # 紧急清理 SQL
    └── clickhouse-maintenance.sh        # 维护脚本包装器
```

## 预期效果

### 日志增长速度对比

| 日志表 | 优化前 | 优化后 | 说明 |
|--------|--------|--------|------|
| `processors_profile_log` | ~100MB/天 | ❌ 已禁用 | 完全停止写入 |
| `trace_log` | ~30MB/天 | ❌ 已禁用 | 完全停止写入 |
| `query_log` | ~50MB/天 | ~2.5MB/天 | 5% 采样 + 7天 TTL |
| `query_thread_log` | ~80MB/天 | ~4MB/天 | 5% 采样 + 3天 TTL |
| `text_log` | ~20MB/天 | ~2MB/天 | ERROR 级别 + 3天 TTL |

### 磁盘空间使用预期

- **优化前**: 每天增长 ~280MB，约 2 个月撑满 Docker 虚拟磁盘（60GB）
- **优化后**: 每天增长 ~8.5MB，稳定在 100-200MB（TTL 自动清理）
- **可用空间**: 宿主机 200GB+，足够长期运行

## 监控建议

### 1. 定期检查磁盘使用

```bash
# 每天快速检查
./scripts/clickhouse-maintenance.sh check

# 查看宿主机目录大小
du -sh /workspace/Documents/clickhouse_data
```

### 2. 监控告警阈值

- **警告**: 宿主机数据目录 > 50GB
- **告警**: 宿主机数据目录 > 100GB
- **紧急**: 宿主机数据目录 > 150GB（考虑清理业务数据或扩容）

### 3. 日志表异常检测

如果某个日志表突然暴涨（> 1GB），检查：

1. TTL 是否正常工作
   ```sql
   SELECT table, ttl_expression FROM system.table_ttls WHERE database='system';
   ```

2. 是否有配置冲突
   ```bash
   docker logs trading_clickhouse --tail 50
   ```

3. 手动触发清理
   ```bash
   ./scripts/clickhouse-maintenance.sh optimize
   ```

## 故障排查

### 问题 1: ClickHouse 启动失败

**检查日志**:
```bash
docker logs trading_clickhouse --tail 100
```

**常见原因**:
- 配置文件格式错误
- 数据目录权限问题
- 端口冲突

### 问题 2: TTL 不生效

**验证 TTL 配置**:
```sql
SELECT table, ttl_expression FROM system.table_ttls WHERE database='system';
```

**手动触发合并**:
```sql
OPTIMIZE TABLE system.query_log FINAL;
```

### 问题 3: 数据目录仍在增长

**检查禁用的日志是否还在写入**:
```sql
SELECT table, sum(rows) AS rows FROM system.parts
WHERE database='system' AND table IN ('processors_profile_log', 'trace_log')
GROUP BY table;
```

如果仍有数据，检查配置文件是否正确加载：
```bash
docker exec trading_clickhouse clickhouse-client --query "SELECT * FROM system.config_files"
```

## 回滚方案

如果需要恢复到旧配置：

1. 恢复旧的配置文件
   ```bash
   mv infra/clickhouse-config/system_logs_ttl.xml.bak infra/clickhouse-config/system_logs_ttl.xml
   mv infra/clickhouse-config/10-logs.xml infra/clickhouse-config/10-logs.xml.disabled
   ```

2. 恢复使用 Docker 卷
   ```yaml
   # infra/compose.yml
   volumes:
     - ch_data:/var/lib/clickhouse  # 改回 Docker 卷
   ```

3. 重启容器
   ```bash
   cd infra && docker compose up -d --force-recreate clickhouse
   ```

## 参考资料

- [ClickHouse 系统日志文档](https://clickhouse.com/docs/en/operations/system-tables/)
- [ClickHouse TTL 文档](https://clickhouse.com/docs/en/engines/table-engines/mergetree-family/mergetree#table_engine-mergetree-ttl)
- [Docker 日志驱动配置](https://docs.docker.com/config/containers/logging/configure/)

## 更新日志

- **2025-10-21**: 初始版本，实施完整的日志管理方案
  - 禁用 `processors_profile_log` 和 `trace_log`
  - 设置 TTL（3-7 天）
  - 配置查询日志采样（5%）
  - 数据迁移到宿主机目录
  - 添加 Docker 日志轮转
  - 创建维护脚本工具
