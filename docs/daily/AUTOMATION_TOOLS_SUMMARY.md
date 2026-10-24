# 自动化运维工具总览

> 所有可自动化的每日检查和监控工具
>
> **创建日期**: 2025-10-28

---

## 📊 工具矩阵

| 工具 | 类型 | 运行频率 | 输出 | 用途 |
|------|------|---------|------|------|
| **system_health_check.py** | 系统健康 | 每小时 | JSON | 整体系统状态 |
| **comprehensive_data_validation.py** | 数据质量 | 每日盘后 | JSON + 终端 | 全面数据检查 |
| **check_ibkr_status.py** | IBKR 连接 | 盘前/实时 | 终端 | 账户和订单状态 |
| **daily_data_check.sh** | 快捷脚本 | 每日盘后 | 终端 | 一键数据检查 |
| **morning_check.sh** | 盘前检查 | 每日盘前 | 终端 | 启动前验证 |
| **evening_review.sh** | 盘后分析 | 每日盘后 | 终端 | 复盘和优化 |

---

## 🔧 工具详解

### 1. 系统健康检查（System Health Check）

**工具**: `docs/daily/system_health_check.py`

**功能**:
- ✅ Docker 容器状态
- ✅ run_system.py 进程检查
- ✅ IBKR 连接状态
- ✅ Kafka Consumer LAG
- ✅ 系统资源（CPU/内存/磁盘）
- ✅ 错误日志统计
- ✅ 数据质量快速检查

**使用**:
```bash
# 手动运行
cd ./docs/daily
./system_health_check.py

# 定时任务（每小时）
0 * * * * cd ./docs/daily && ./system_health_check.py
```

**输出**:
- `logs/system_health.json` - 固定文件名，便于 AI 读取
- `logs/health_history/system_health_YYYYMMDD_HHMMSS.json` - 历史存档

---

### 2. 全面数据质量检查（Comprehensive Data Validation）

**工具**: `tools/comprehensive_data_validation.py`

**功能**:
- ✅ **完整性**: Bar 数量、Tick 数据量
- ✅ **连续性**: 时间间隔、重复检测
- ✅ **特征**: Features 覆盖率、准确性
- ✅ **准确性**: OHLC 一致性、价格异常
- ✅ **基础设施**: Kafka LAG、处理延迟

**使用**:
```bash
# 快捷脚本（推荐）
./scripts/daily_data_check.sh

# 完整检查
./scripts/daily_data_check.sh --full

# 自定义
python tools/comprehensive_data_validation.py \
  --date 2025-10-27 \
  --symbols AAPL,TSLA \
  --checks completeness,continuity,features \
  --output reports/data_quality.json
```

**输出**:
- `reports/data_quality_YYYY-MM-DD.json` - 结构化报告
- 终端输出 - 汇总和问题列表

---

### 3. IBKR 状态检查（IBKR Status Check）

**工具**: `temp/check_ibkr_status.py`

**功能**:
- ✅ 当前持仓
- ✅ 今日成交
- ✅ 未成交订单
- ✅ 所有订单历史
- ✅ 账户余额
- ✅ 盈亏统计

**使用**:
```bash
python temp/check_ibkr_status.py
```

**输出**: 终端格式化输出，包含表情符号

---

## 📅 每日运维流程

### 盘前 (07:00-09:30 ET)

```bash
# 1. 系统健康检查
cd ./docs/daily
./system_health_check.py

# 2. IBKR 连接检查
cd .
python temp/check_ibkr_status.py

# 3. 昨日数据快速检查
./scripts/daily_data_check.sh
```

**自动化脚本**: `docs/daily/morning_check.sh`

---

### 盘中 (09:30-16:00 ET)

```bash
# 1. 实时监控（每小时自动运行）
# Cron: 0 * * * * cd ./docs/daily && ./system_health_check.py

# 2. 如有告警，手动检查
cat logs/system_health.json | jq '.checks'

# 3. IBKR 状态（按需）
python temp/check_ibkr_status.py
```

---

### 盘后 (16:00-20:00 ET)

```bash
# 1. 完整数据质量检查
./scripts/daily_data_check.sh --full

# 2. 查看报告
cat reports/data_quality_$(date -v-1d +%Y-%m-%d).json | jq '.summary'

# 3. 如有问题，详细分析
cat reports/data_quality_*.json | jq '.checks[][] | select(.status == "error")'

# 4. Alpha Loop 优化（可选）
# 见 evening_review.sh
```

**自动化脚本**: `docs/daily/evening_review.sh`

---

## 🤖 完全自动化配置

### 1. Crontab 配置

```bash
crontab -e

# 系统健康检查 - 每小时
0 * * * * cd ./docs/daily && ./system_health_check.py >> ./logs/health_cron.log 2>&1

# 数据质量检查 - 每日盘后 5PM
0 17 * * 1-5 cd . && ./scripts/daily_data_check.sh --full >> ./logs/data_validation_cron.log 2>&1

# 盘前检查 - 每日早上 8AM
0 8 * * 1-5 cd ./docs/daily && ./morning_check.sh >> ./logs/morning_cron.log 2>&1
```

### 2. 集成到现有脚本

**在 `morning_check.sh` 中添加**:
```bash
# 运行系统健康检查
echo "Running system health check..."
cd ./docs/daily
./system_health_check.py

# 检查昨日数据
echo "Validating yesterday's data..."
cd .
./scripts/daily_data_check.sh
```

**在 `evening_review.sh` 中添加**:
```bash
# 完整数据质量检查
echo "=========================================="
echo "Data Quality Validation"
echo "=========================================="
./scripts/daily_data_check.sh --full

# 如果失败，发送告警
if [ $? -ne 0 ]; then
    echo "⚠️  Data quality issues detected!"
    # 可以在这里添加告警逻辑（Slack/Email）
fi
```

---

## 📈 告警配置示例

### 1. 基于 JSON 的自动告警

```python
#!/usr/bin/env python3
"""Auto-alert based on health check results"""

import json
import sys
from datetime import datetime

# Read system health
with open('./logs/system_health.json') as f:
    health = json.load(f)

# Read data quality
with open('./reports/data_quality_latest.json') as f:
    data_quality = json.load(f)

# Check for critical issues
critical_issues = []

if health['overall_status'] == 'error':
    critical_issues.append(f"System health: {health['summary']['errors']} errors")

if data_quality['summary']['critical_errors'] > 0:
    critical_issues.append(f"Data quality: {data_quality['summary']['critical_errors']} critical errors")

# Send alert if needed
if critical_issues:
    # Send to Slack/Email/PagerDuty
    print("🔴 CRITICAL ALERTS:")
    for issue in critical_issues:
        print(f"  - {issue}")
    sys.exit(1)
else:
    print("✅ All systems nominal")
    sys.exit(0)
```

### 2. Slack 集成示例

```bash
#!/bin/bash
# Send Slack notification if checks fail

SLACK_WEBHOOK="https://hooks.slack.com/services/YOUR/WEBHOOK/URL"

# Run health check
./system_health_check.py
if [ $? -ne 0 ]; then
    curl -X POST $SLACK_WEBHOOK \
      -H 'Content-Type: application/json' \
      -d '{"text":"🔴 System health check failed!"}'
fi

# Run data validation
./scripts/daily_data_check.sh --full
if [ $? -ne 0 ]; then
    curl -X POST $SLACK_WEBHOOK \
      -H 'Content-Type: application/json' \
      -d '{"text":"⚠️ Data quality issues detected!"}'
fi
```

---

## 📊 报告查询速查

### System Health JSON

```bash
# 总体状态
jq '.overall_status' logs/system_health.json

# Docker 容器状态
jq '.checks.docker_containers' logs/system_health.json

# IBKR 连接状态
jq '.checks.ibkr_connection' logs/system_health.json

# Kafka Consumer LAG
jq '.checks.kafka_consumers.high_lag_groups' logs/system_health.json

# 所有错误
jq '.checks | to_entries[] | select(.value.status == "error")' logs/system_health.json
```

### Data Quality JSON

```bash
# 总体汇总
jq '.summary' reports/data_quality_20251027.json

# 特定股票的所有检查
jq '.checks.AAPL' reports/data_quality_20251027.json

# 所有失败的检查
jq '.checks[][] | select(.status != "pass")' reports/data_quality_20251027.json

# 数据完整性检查
jq '.checks[].bar_completeness_1m' reports/data_quality_20251027.json

# Feature 覆盖率
jq '.checks[].feature_completeness.coverage_pct' reports/data_quality_20251027.json
```

---

## 🎯 最佳实践

### 1. 每日必做（自动化）

- ✅ **盘前**: 运行 `morning_check.sh` 或 `system_health_check.py`
- ✅ **盘中**: 定时系统健康检查（每小时）
- ✅ **盘后**: 运行 `daily_data_check.sh --full`

### 2. 按需手动检查

- 🔍 **策略调试**: 检查特定股票的 features
- 🔍 **数据问题**: 运行完整验证并查看 JSON 报告
- 🔍 **性能问题**: 检查 Kafka LAG 和系统资源

### 3. 问题处理流程

```
1. 检测到问题（自动告警或定时检查）
   ↓
2. 查看 JSON 报告，定位具体问题
   ↓
3. 运行针对性检查（--checks specific_type）
   ↓
4. 修复问题（补数据/重启服务/修复配置）
   ↓
5. 重新运行验证，确认修复
```

---

## 📁 文件位置总览

```
./
├── docs/daily/
│   ├── system_health_check.py          ← 系统健康检查
│   ├── HEALTH_CHECK_GUIDE.md           ← 健康检查指南
│   ├── DATA_VALIDATION_GUIDE.md        ← 数据验证指南
│   ├── DAILY_OPERATIONS_CHECKLIST.md   ← 每日运维清单
│   ├── QUICK_REFERENCE.md              ← 快速参考
│   ├── morning_check.sh                ← 盘前检查脚本
│   ├── evening_review.sh               ← 盘后复盘脚本
│   └── AUTOMATION_TOOLS_SUMMARY.md     ← 本文档
├── tools/
│   ├── comprehensive_data_validation.py ← 全面数据验证
│   └── validate_data.py                ← 基础数据验证（原有）
├── scripts/
│   └── daily_data_check.sh             ← 快捷检查脚本
├── temp/
│   └── check_ibkr_status.py            ← IBKR 状态检查
├── logs/
│   ├── system_health.json              ← 最新健康报告
│   └── health_history/                 ← 历史健康报告
└── reports/
    └── data_quality_*.json             ← 数据质量报告
```

---

## 🔄 升级路径

### 从基础检查迁移

如果你之前使用 `validate_data.py`，可以这样升级：

```bash
# 旧方式（仍然可用）
python tools/validate_data.py \
  --date 2025-10-27 \
  --symbols AAPL,TSLA \
  --checks gaps,nulls,outliers,freshness

# 新方式（推荐）
python tools/comprehensive_data_validation.py \
  --date 2025-10-27 \
  --symbols AAPL,TSLA \
  --full

# 或使用快捷脚本
./scripts/daily_data_check.sh
```

**建议**: 保留 `validate_data.py` 用于快速简单检查，使用 `comprehensive_data_validation.py` 进行完整验证。

---

**最后更新**: 2025-10-28
**维护者**: Trading Team
**文档版本**: 1.0
