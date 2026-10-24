# Mac Air 迁移方案（实操版）

更新时间：2026-02-26  
适用仓库：`.`

## 1. 当前状态（已确认）

- 项目总大小约：`37G`
- `backup/` 总大小约：`31G`
- 可用数据库备份（推荐使用最新一份）：
  - ClickHouse：`backup/clickhouse/2026-02-24`（约 `9.3G`，含 `restore.sh`）
  - PostgreSQL：`backup/pg/2026-02-24`（约 `216K`，含 `00_globals.sql.gz` 和每库备份）

## 2. 迁移策略建议

建议采用“**代码 + 最新数据库备份**”迁移，不要原样拷贝整个 `37G` 目录。

原因：
- `venv/`、`venv_ml/` 可在新机器重建（约 `2.9G`）
- `logs/`、`temp/` 历史文件可不迁移（约 `2.3G`）
- `backup/` 里旧日期备份可不迁移（节省大量空间）

## 3. 需要复制的内容

### 必选

- 项目代码（排除虚拟环境和缓存）
- 配置文件：`.env`、`config.yml`
- 数据库备份：
  - `backup/clickhouse/2026-02-24/`
  - `backup/pg/2026-02-24/`

### 可选

- `models/`（如要保留已有模型文件）
- `store/`（如有本地状态）
- `runs/`（如要保留运行记录）

## 4. 旧机器打包命令

在当前机器执行：

```bash
cd /workspace/Documents

# 1) 打包项目代码（排除大体积可重建目录）
tar -czf trading_code_$(date +%Y%m%d_%H%M%S).tar.gz \
  --exclude='trading/.git' \
  --exclude='trading/venv' \
  --exclude='trading/venv_ml' \
  --exclude='trading/__pycache__' \
  --exclude='trading/.pytest_cache' \
  --exclude='trading/logs' \
  --exclude='trading/temp' \
  --exclude='trading/backup' \
  trading

# 2) 单独打包最新数据库备份
tar -czf trading_db_backup_2026-02-24.tar.gz \
  -C . backup/clickhouse/2026-02-24 backup/pg/2026-02-24
```

传输建议（任选其一）：

```bash
# 方案A：局域网 rsync（推荐）
rsync -avh --progress ._code_*.tar.gz <air_user>@<air_ip>:~/
rsync -avh --progress ._db_backup_2026-02-24.tar.gz <air_user>@<air_ip>:~/

# 方案B：移动硬盘
cp ._code_*.tar.gz /Volumes/<DISK>/
cp ._db_backup_2026-02-24.tar.gz /Volumes/<DISK>/
```

## 5. Mac Air 恢复步骤

```bash
# 1) 解压到目标目录
cd ~/Documents
tar -xzf ~/trading_code_*.tar.gz
cd ~/Documents/trading

# 2) 解压数据库备份
tar -xzf ~/trading_db_backup_2026-02-24.tar.gz -C ~/Documents/trading
```

## 6. Mac Air 环境准备

```bash
# Homebrew (若未安装)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 基础依赖
brew install python@3.11 docker docker-compose postgresql redis

# 启动 Docker Desktop（图形界面打开一次）
open -a Docker
```

Python 环境：

```bash
cd ~/Documents/trading
python3 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

## 7. 配置路径修正（关键）

`infra/compose.yml` 已改为使用 `${HOME}` 自动解析 ClickHouse 路径：
- `${HOME}/Documents/clickhouse_data`
- `${HOME}/Documents/clickhouse_logs`

仍需确保目录存在：

```bash
mkdir -p /Users/<air_user>/Documents/clickhouse_data
mkdir -p /Users/<air_user>/Documents/clickhouse_logs
```

或直接运行：

```bash
cd ~/Documents/trading
bash scripts/migration/setup_airmac.sh
```

## 8. 启动服务与恢复数据库

```bash
cd ~/Documents/trading
make up
```

### 恢复 ClickHouse

```bash
cd ~/Documents/trading/backup/clickhouse/2026-02-24
bash restore.sh
```

### 恢复 PostgreSQL

```bash
cd ~/Documents/trading/backup/pg/2026-02-24

# 1) 全局对象（角色等）
gunzip -c 00_globals.sql.gz | psql -U postgres -h localhost -p 5432

# 2) 每个库恢复（会调用各目录下 restore.sh）
for d in */; do
  [ -f "${d}restore.sh" ] || continue
  (cd "$d" && bash restore.sh)
done
```

## 9. 验证清单

```bash
cd ~/Documents/trading
make verify

# 可选：测试
make test
```

重点检查：
- `docker compose -f infra/compose.yml ps` 全部服务 `Up`
- ClickHouse 查询返回正常（端口 `8123/9000`）
- `make verify` 中 Postgres/Redis/Kafka/Grafana 无 FAIL

## 10. 回滚方案

如果 Mac Air 启动异常，直接保留旧机器继续运行，Mac Air 做离线修复：

1. `make down`
2. 修正 `infra/compose.yml` 路径或端口冲突
3. 重新 `make up` + 恢复脚本

## 11. 安全提醒（必须做）

- 不要把 `.env` 上传到公开网盘/Git 仓库。
- 若 `.env` 曾在不可信通道传输，建议轮换 API keys（OpenAI、Gemini、DeepSeek、Telegram 等）。
