# 系统健康检查使用指南

## 📋 概述

`system_health_check.py` 是一个自动化的系统监测脚本，执行所有关键检查并生成 JSON 报告。

**报告位置**:
- 最新报告（固定文件名）: `./logs/system_health.json`
- 历史记录: `./logs/health_history/system_health_YYYYMMDD_HHMMSS.json`

---

## 🚀 使用方法

### 基本用法

```bash
cd ./docs/daily
./system_health_check.py
```

### 显示详细日志

```bash
./system_health_check.py --verbose
# 或
./system_health_check.py -v
```

### 在其他脚本中调用

```bash
# 运行检查并保存报告
python ./docs/daily/system_health_check.py

# 检查退出码
if [ $? -eq 0 ]; then
    echo "系统健康"
elif [ $? -eq 2 ]; then
    echo "系统有警告"
else
    echo "系统有错误"
fi
```

---

## 📊 检查项目

脚本会自动检查以下 7 个方面：

### 1. **Docker 容器状态**
- 检查所有容器是否运行
- 验证健康状态
- 关键容器: kafka, clickhouse, redis

**健康标准**:
- ✅ 所有容器 State=running
- ✅ Health=healthy 或为空

### 2. **run_system.py 运行状态**
- 检查进程是否运行
- 统计最近错误数量

**健康标准**:
- ✅ 进程正在运行
- ✅ 最近错误 < 10 个

### 3. **IBKR 连接状态**
- 调用 check_ibkr_status.py
- 验证连接是否正常

**健康标准**:
- ✅ IBKR Connected = True

### 4. **数据质量**
- 最近 10 分钟的 Tick 数据
- 昨日 Bar 数据完整性（应 ≥ 390）

**健康标准**:
- ✅ 有实时 Tick 数据
- ✅ 昨日 Bar ≥ 390 条

### 5. **Kafka Consumers**
- 检查所有 consumer groups
- 统计 LAG

**健康标准**:
- ✅ LAG < 1000 (健康)
- ⚠️ 1000 ≤ LAG < 10000 (警告)
- ❌ LAG ≥ 10000 (错误)

### 6. **错误日志**
- 扫描最近 200 行日志
- 统计 ERROR 和 CRITICAL

**健康标准**:
- ✅ CRITICAL = 0
- ⚠️ ERROR < 20

### 7. **系统资源**
- CPU 使用率
- 内存使用率
- 磁盘使用率

**健康标准**:
- ✅ CPU < 90%
- ✅ 内存 < 90%
- ✅ 磁盘 < 90%

---

## 📄 JSON 报告格式

```json
{
  "timestamp": "2025-10-27T23:59:00.123456",
  "timestamp_readable": "2025-10-27 23:59:00 EST",
  "overall_status": "healthy",  // healthy, warning, error
  "checks": {
    "docker_containers": {
      "status": "healthy",
      "containers": {
        "trading_kafka": {
          "state": "running",
          "health": "healthy",
          "status": "healthy"
        },
        "trading_clickhouse": { ... },
        ...
      },
      "message": ""
    },
    "run_system_py": {
      "status": "healthy",
      "running": true,
      "pid": 12345,
      "recent_errors": 3,
      "message": ""
    },
    "ibkr_connection": {
      "status": "healthy",
      "connected": true,
      "message": ""
    },
    "data_quality": {
      "status": "healthy",
      "recent_ticks": {
        "AAPL": 1234,
        "TSLA": 2345
      },
      "yesterday_bars": {
        "AAPL": { "count": 390, "complete": true },
        "TSLA": { "count": 390, "complete": true }
      },
      "message": ""
    },
    "kafka_consumers": {
      "status": "healthy",
      "groups": {
        "lake_writer_quotes": { "lag": 119, "status": "healthy" },
        ...
      },
      "high_lag_groups": [],
      "message": ""
    },
    "recent_errors": {
      "status": "healthy",
      "error_count": 5,
      "critical_count": 0,
      "recent_errors": [
        "2025-10-27 10:00:00 ERROR ...",
        ...
      ],
      "message": ""
    },
    "system_resources": {
      "status": "healthy",
      "cpu_percent": 45.2,
      "memory_percent": 62.3,
      "disk_percent": 55.8,
      "message": ""
    }
  },
  "summary": {
    "total_checks": 7,
    "passed": 7,
    "warnings": 0,
    "errors": 0
  }
}
```

---

## 🤖 AI 读取示例

### Python

```python
import json
from pathlib import Path

# 读取最新报告
report_file = Path("./logs/system_health.json")
with open(report_file) as f:
    report = json.load(f)

# 检查总体状态
if report["overall_status"] == "healthy":
    print("✅ 系统健康")
elif report["overall_status"] == "warning":
    print("⚠️ 系统有警告")
    # 查看警告详情
    for check_name, check_result in report["checks"].items():
        if check_result.get("status") == "warning":
            print(f"  {check_name}: {check_result.get('message')}")
else:
    print("❌ 系统有错误")
    # 查看错误详情
    for check_name, check_result in report["checks"].items():
        if check_result.get("status") == "error":
            print(f"  {check_name}: {check_result.get('message')}")

# 检查数据质量
data_quality = report["checks"]["data_quality"]
print(f"\n最近 Tick 数据: {data_quality['recent_ticks']}")
print(f"昨日 Bar 完整性: {data_quality['yesterday_bars']}")

# 检查 Kafka LAG
kafka = report["checks"]["kafka_consumers"]
if kafka["high_lag_groups"]:
    print(f"\n⚠️ 高延迟 Consumer Groups: {kafka['high_lag_groups']}")

# 检查系统资源
resources = report["checks"]["system_resources"]
print(f"\nCPU: {resources['cpu_percent']:.1f}%")
print(f"内存: {resources['memory_percent']:.1f}%")
print(f"磁盘: {resources['disk_percent']:.1f}%")
```

### Shell Script

```bash
#!/bin/bash

# 运行健康检查
python ./docs/daily/system_health_check.py

# 读取 JSON 报告
REPORT="./logs/system_health.json"

# 提取总体状态
STATUS=$(jq -r '.overall_status' "$REPORT")

if [ "$STATUS" == "healthy" ]; then
    echo "✅ 系统健康"
    exit 0
elif [ "$STATUS" == "warning" ]; then
    echo "⚠️ 系统有警告"
    jq -r '.checks | to_entries[] | select(.value.status == "warning") | "\(.key): \(.value.message)"' "$REPORT"
    exit 2
else
    echo "❌ 系统有错误"
    jq -r '.checks | to_entries[] | select(.value.status == "error") | "\(.key): \(.value.message)"' "$REPORT"
    exit 1
fi
```

---

## 🔄 定时执行

### 添加到 crontab

```bash
# 编辑 crontab
crontab -e

# 每小时执行一次健康检查
0 * * * * ./docs/daily/system_health_check.py >> ./logs/health_check.log 2>&1

# 每 15 分钟执行一次
*/15 * * * * ./docs/daily/system_health_check.py >> ./logs/health_check.log 2>&1
```

### 整合到 morning_check.sh

```bash
# 在 morning_check.sh 中添加
echo "运行系统健康检查..."
python ./docs/daily/system_health_check.py

# 检查结果
if [ $? -ne 0 ]; then
    echo "⚠️ 发现问题，请查看报告: ./logs/system_health.json"
fi
```

---

## 📊 状态码说明

脚本退出时返回不同的状态码：

| 状态码 | 含义 | 说明 |
|--------|------|------|
| 0 | 健康 | 所有检查通过 |
| 2 | 警告 | 有警告但不影响运行 |
| 1 | 错误 | 有严重错误需要修复 |

---

## 🛠️ 依赖安装

脚本需要 `psutil` 库：

```bash
# 安装依赖
pip install psutil

# 或使用 venv
cd .
venv/bin/pip install psutil
```

---

## 💡 使用建议

### 每日运维

1. **盘前检查** (07:00):
   ```bash
   ./system_health_check.py
   cat logs/system_health.json | jq '.overall_status'
   ```

2. **盘中监控** (每小时):
   ```bash
   # 通过 cron 自动运行
   # 出现警告时查看报告
   ```

3. **盘后检查** (16:30):
   ```bash
   ./system_health_check.py -v
   ```

### AI 辅助分析

```bash
# 生成报告
./system_health_check.py

# 将报告内容提供给 AI
cat ./logs/system_health.json | pbcopy

# 提示词：
# "请分析这个系统健康报告，告诉我需要关注哪些问题，以及如何修复。"
```

---

## 🔍 故障排查

### 问题：psutil 导入失败

```bash
pip install psutil
# 或
venv/bin/pip install psutil
```

### 问题：无法连接 ClickHouse

```bash
# 检查容器
cd ./infra
docker-compose ps

# 重启
docker-compose restart clickhouse
```

### 问题：JSON 报告为空

```bash
# 检查日志目录权限
ls -la ./logs/

# 创建目录
mkdir -p ./logs/health_history
```

---

## 📚 相关文档

- **快速参考**: [QUICK_REFERENCE.md](QUICK_REFERENCE.md)
- **完整清单**: [DAILY_OPERATIONS_CHECKLIST.md](DAILY_OPERATIONS_CHECKLIST.md)
- **工具包说明**: [README.md](README.md)

---

**最后更新**: 2025-10-27
**维护者**: Trading Team
