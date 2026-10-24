# 系统健康检查 - 快速上手

## 🎯 什么是系统健康检查？

一个自动化脚本，一键检查系统所有关键组件，生成 JSON 报告，方便 AI 读取和分析。

---

## ⚡ 快速使用

### 1. 安装依赖（首次运行）

```bash
cd ./docs/daily
./install_dependencies.sh
```

### 2. 运行健康检查

```bash
./system_health_check.py
```

**输出示例**:
```
🔍 开始系统健康检查...

  ⏳ 检查 Docker 容器... ✅
  ⏳ 检查 run_system.py... ✅
  ⏳ 检查 IBKR 连接... ✅
  ⏳ 检查 数据质量... ✅
  ⏳ 检查 Kafka Consumers... ✅
  ⏳ 检查 错误日志... ✅
  ⏳ 检查 系统资源... ✅

============================================================
📊 系统健康检查报告
============================================================
检查时间: 2025-10-28 00:00:00 EST

总体状态: ✅ HEALTHY

检查项目: 7
  ✅ 通过: 7
  ⚠️  警告: 0
  ❌ 错误: 0

============================================================

📄 完整报告: ./logs/system_health.json
📂 历史记录: ./logs/health_history/system_health_20251028_000000.json
```

### 3. 查看 JSON 报告

```bash
# 查看最新报告
cat ./logs/system_health.json | jq .

# 查看总体状态
cat ./logs/system_health.json | jq '.overall_status'

# 查看各项检查结果
cat ./logs/system_health.json | jq '.checks'
```

---

## 📊 JSON 报告示例

```json
{
  "timestamp": "2025-10-28T00:00:00.123456",
  "timestamp_readable": "2025-10-28 00:00:00 EST",
  "overall_status": "healthy",
  "checks": {
    "docker_containers": {
      "status": "healthy",
      "containers": {
        "trading_kafka": {
          "state": "running",
          "health": "healthy",
          "status": "healthy"
        }
      }
    },
    "run_system_py": {
      "status": "healthy",
      "running": true,
      "pid": 12345,
      "recent_errors": 2
    },
    "data_quality": {
      "status": "healthy",
      "recent_ticks": {
        "AAPL": 1234,
        "TSLA": 2345
      },
      "yesterday_bars": {
        "AAPL": { "count": 390, "complete": true }
      }
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

## 🤖 AI 使用示例

### Python

```python
import json

# 读取报告
with open("./logs/system_health.json") as f:
    report = json.load(f)

# 分析状态
if report["overall_status"] == "healthy":
    print("✅ 系统正常运行")
else:
    print(f"⚠️ 系统状态: {report['overall_status']}")
    
    # 查看问题详情
    for name, check in report["checks"].items():
        if check["status"] != "healthy":
            print(f"  - {name}: {check.get('message', '无详细信息')}")

# 检查数据质量
ticks = report["checks"]["data_quality"]["recent_ticks"]
print(f"\n最近 Tick 数据: {sum(ticks.values())} 条")

# 检查系统资源
resources = report["checks"]["system_resources"]
print(f"CPU: {resources['cpu_percent']:.1f}%")
print(f"内存: {resources['memory_percent']:.1f}%")
```

### Shell Script

```bash
#!/bin/bash

# 运行健康检查
./docs/daily/system_health_check.py

# 获取状态
STATUS=$(jq -r '.overall_status' ./logs/system_health.json)

if [ "$STATUS" == "healthy" ]; then
    echo "✅ 系统健康"
    exit 0
else
    echo "⚠️ 系统有问题，请检查报告"
    exit 1
fi
```

---

## 🔄 定时执行

### 方法 1: Crontab

```bash
# 编辑 crontab
crontab -e

# 每小时执行一次
0 * * * * ./docs/daily/system_health_check.py >> ./logs/health_check.log 2>&1
```

### 方法 2: 整合到现有脚本

在 `morning_check.sh` 或 `evening_review.sh` 中添加：

```bash
# 运行健康检查
echo "运行系统健康检查..."
./docs/daily/system_health_check.py

# 检查结果
if [ $? -ne 0 ]; then
    echo "⚠️ 发现问题，请查看报告"
fi
```

---

## 📋 检查项目详解

| 项目 | 检查内容 | 健康标准 |
|------|---------|---------|
| **Docker 容器** | 所有容器运行状态 | State=running, Health=healthy |
| **run_system.py** | 进程运行、错误数量 | 进程运行中, 错误 < 10 |
| **IBKR 连接** | 连接状态 | Connected = True |
| **数据质量** | Tick 数据、Bar 完整性 | 有实时数据, Bar ≥ 390 |
| **Kafka Consumers** | Consumer Groups LAG | LAG < 1000 |
| **错误日志** | ERROR/CRITICAL 统计 | CRITICAL = 0, ERROR < 20 |
| **系统资源** | CPU/内存/磁盘 | 均 < 90% |

---

## 🔍 常见问题

### Q1: psutil 导入失败？

```bash
./install_dependencies.sh
```

### Q2: 报告文件不存在？

```bash
mkdir -p ./logs/health_history
```

### Q3: 如何查看历史报告？

```bash
ls -lt ./logs/health_history/
cat ./logs/health_history/system_health_20251028_000000.json | jq .
```

### Q4: 如何让 AI 读取报告？

报告文件固定为 `system_health.json`，直接读取即可：

```python
import json

with open("./logs/system_health.json") as f:
    report = json.load(f)
    
# AI 可以直接分析 report 字典
```

---

## 📚 更多文档

- **完整指南**: [HEALTH_CHECK_GUIDE.md](HEALTH_CHECK_GUIDE.md)
- **快速参考**: [QUICK_REFERENCE.md](QUICK_REFERENCE.md)
- **工具包说明**: [README.md](README.md)

---

**创建时间**: 2025-10-28
**脚本位置**: `./docs/daily/system_health_check.py`
**报告位置**: `./logs/system_health.json`
