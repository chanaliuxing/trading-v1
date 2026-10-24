# 全面数据质量检查指南

## 📋 概述

`comprehensive_data_validation.py` 是一个全面的数据质量检查工具，远超原有的 `validate_data.py`。

### ✅ 新增功能对比

| 功能 | validate_data.py | comprehensive_data_validation.py |
|------|-----------------|--------------------------------|
| 数据完整性 | ✅ 基础检查 | ✅ 多分辨率检查（1m/5m/15m/1h/1d） |
| NULL 值检查 | ✅ | ✅ |
| 离群值检查 | ✅ 基础统计 | ✅ 智能价格波动检测 |
| 数据新鲜度 | ✅ | ✅ |
| **数据连续性** | ❌ | ✅ 时间间隔检查、重复检测 |
| **特征完整性** | ❌ | ✅ Features 计算覆盖率 |
| **特征准确性** | ❌ | ✅ EMA/指标合理性验证 |
| **OHLC 一致性** | ❌ | ✅ High >= Low 等逻辑检查 |
| **价格异常波动** | ❌ | ✅ 单K线>10%变动检测 |
| **Tick 数据量** | ❌ | ✅ Trades/Quotes 数量验证 |
| **Kafka 延迟** | ❌ | ✅ Consumer Group LAG 监控 |
| **JSON 报告** | ❌ | ✅ 结构化输出，便于自动化 |

---

## 🚀 快速开始

### 1. 每日快速检查（推荐）

```bash
cd .

# 检查昨天的主要股票
python tools/comprehensive_data_validation.py \
  --date $(date -v-1d +%Y-%m-%d) \
  --symbols AAPL,TSLA,NVDA,SPY \
  --quick

# 或使用快捷别名
./scripts/daily_data_check.sh
```

### 2. 完整验证（盘后）

```bash
# 全面检查所有维度
python tools/comprehensive_data_validation.py \
  --date 2025-10-27 \
  --symbols AAPL,TSLA,NVDA,GOOGL,MSFT,META,AMZN,SPY,QQQ \
  --full \
  --output reports/data_quality_$(date +%Y%m%d).json
```

### 3. 针对性检查

```bash
# 只检查特征计算
python tools/comprehensive_data_validation.py \
  --symbols AAPL,TSLA \
  --checks features

# 只检查数据连续性
python tools/comprehensive_data_validation.py \
  --symbols AAPL \
  --checks continuity
```

---

## 📊 检查类型详解

### 1. Completeness（完整性检查）

#### 1.1 Bar 数据完整性
- **检查内容**: 每个分辨率的 K 线数量是否符合预期
- **预期值**:
  - 1m: 390 bars (9:30-16:00)
  - 5m: 78 bars
  - 15m: 26 bars
  - 1h: 7 bars
  - 1d: 1 bar
- **阈值**:
  - ≥100%: ✅ Pass
  - 95-99%: ⚠️ Warning
  - <95%: ❌ Error
  - 0%: 🔴 Critical

**示例**:
```
✅ AAPL 1m: Complete: 390/390 bars
⚠️ TSLA 5m: Nearly complete: 76/78 bars (97.44%)
❌ NVDA 1m: Incomplete: 350/390 bars (89.74%)
```

#### 1.2 Tick 数据量检查
- **检查内容**: Trades 和 Quotes 数量
- **启发式规则**:
  - Active stocks: ≥10,000 trades/day
  - Low volume: 1,000-10,000 trades
  - Very low: <1,000 trades
  - Critical: 0 trades

**示例**:
```
✅ AAPL: Good volume: 125,432 trades, 1,245,678 quotes
⚠️ TSLA: Low volume: 5,234 trades, 52,341 quotes
```

---

### 2. Continuity（连续性检查）

#### 2.1 时间间隔检查
- **检查内容**: 检测 K 线时间序列中的异常间隔
- **预期**: 1m bars 每分钟一个，不应有>1分钟的gap
- **报告**: 列出最大的10个gap

**示例**:
```
✅ AAPL 1m: No unexpected gaps
⚠️ TSLA 1m: Found 2 small gaps (max: 3 min)
  - 2025-10-27 10:15:00 → 10:18:00 (3 min gap)
  - 2025-10-27 14:20:00 → 14:22:00 (2 min gap)
```

#### 2.2 重复时间戳检查
- **检查内容**: 同一时间戳是否有多条记录
- **预期**: 每个 ts_start 应该唯一
- **结果**: 任何重复都视为 Error

**示例**:
```
✅ AAPL 1m: No duplicates found
❌ NVDA 1m: Found 3 duplicate timestamps
  - 2025-10-27 10:30:00: 2 records
  - 2025-10-27 11:45:00: 3 records
```

---

### 3. Features（特征验证）

#### 3.1 特征完整性检查
- **检查内容**: bars_1m_current (materialized view) 中有多少bar有计算好的features
- **计算**: `(features_count / bars_count) * 100%`
- **阈值**:
  - ≥99%: ✅ Pass
  - 90-99%: ⚠️ Warning
  - <90%: ❌ Error

**示例**:
```
✅ AAPL: Complete: 390/390 bars have features
⚠️ TSLA: Mostly complete: 95.5% coverage (373/390)
❌ NVDA: Incomplete: 75.0% coverage (293/390)
```

#### 3.2 特征准确性检查
- **检查内容**: EMA 等指标是否在合理范围内
- **验证规则**: EMA 应该在 close * 0.5 到 close * 1.5 之间
- **结果**: 发现异常值即报 Warning

**示例**:
```
✅ AAPL: Feature values look reasonable
⚠️ TSLA: Found 2 suspicious feature values
  - 2025-10-27 10:30:00: ema_9 = 250 (close = 180, ratio = 1.39)
```

---

### 4. Accuracy（准确性验证）

#### 4.1 OHLC 一致性检查
- **检查内容**:
  - High >= Low
  - High >= Open
  - High >= Close
  - Low <= Open
  - Low <= Close
  - All > 0
- **结果**: 任何违反都是 🔴 Critical

**示例**:
```
✅ AAPL 1m: All OHLC values consistent
🔴 TSLA 1m: Found 1 inconsistent bar
  - 2025-10-27 10:30:00: high (180) < low (182) ← 数据错误！
```

#### 4.2 价格异常波动检查
- **检查内容**: 单根K线涨跌幅 >10%
- **目的**: 检测数据错误或真实的extreme move
- **阈值**: 默认 10%，可调整

**示例**:
```
✅ AAPL 1m: No unusual spikes (>10%)
⚠️ NVDA 1m: Found 1 potential spike (max: 12.5%)
  - 2025-10-27 11:30:00: $180 → $202.50 (+12.5%)
    ↑ 需要人工确认是真实波动还是数据错误
```

---

### 5. Infrastructure（基础设施检查）

#### 5.1 Kafka Consumer Lag
- **检查内容**: 所有 consumer groups 的 LAG
- **阈值**: LAG > 1000 报 Warning
- **目的**: 确保数据处理及时

**示例**:
```
✅ All consumer groups healthy
⚠️ Found 2 groups with high lag:
  - ch_writer_bars_1m_ibkr: LAG 1,234
  - backfill.5m.v1: LAG 2,500
```

---

## 🔧 高级用法

### 1. 整合到每日脚本

在 `evening_review.sh` 中添加:

```bash
#!/bin/bash

echo "=========================================="
echo "Running Daily Data Validation"
echo "=========================================="

# 检查昨天的数据
YESTERDAY=$(date -v-1d +%Y-%m-%d)

python tools/comprehensive_data_validation.py \
  --date $YESTERDAY \
  --symbols AAPL,TSLA,NVDA,GOOGL,MSFT,META,AMZN,SPY,QQQ \
  --full \
  --output reports/data_quality_$YESTERDAY.json

# 检查退出码
if [ $? -ne 0 ]; then
    echo "⚠️  Data validation found issues!"
    echo "Review report: reports/data_quality_$YESTERDAY.json"
else
    echo "✅ All data quality checks passed"
fi
```

### 2. 定时任务（Cron）

```bash
# 每天盘后 5PM 自动运行
0 17 * * 1-5 cd . && \
  python tools/comprehensive_data_validation.py \
  --date $(date -v-1d +%Y%m%d) \
  --symbols AAPL,TSLA,NVDA,SPY,QQQ \
  --full \
  --output reports/data_quality_$(date -v-1d +%Y%m%d).json \
  >> logs/data_validation.log 2>&1
```

### 3. 自动告警

```python
#!/usr/bin/env python3
"""Parse data validation report and send alerts"""

import json
import sys

report_file = sys.argv[1]
with open(report_file) as f:
    report = json.load(f)

if report["summary"]["critical_errors"] > 0:
    # Send critical alert
    print("🔴 CRITICAL DATA ISSUES!")
    # Send to Slack/Email/PagerDuty
    sys.exit(1)
elif report["summary"]["errors"] > 0:
    print("⚠️  Data validation warnings")
    # Send to Slack
    sys.exit(0)
else:
    print("✅ Data quality OK")
    sys.exit(0)
```

---

## 📈 报告格式

### JSON 输出结构

```json
{
  "timestamp": "2025-10-27T17:00:00",
  "date": "2025-10-27",
  "symbols": ["AAPL", "TSLA"],
  "checks": {
    "AAPL": {
      "bar_completeness_1m": {
        "status": "pass",
        "expected": 390,
        "actual": 390,
        "missing": 0,
        "completeness_pct": 100.0,
        "message": "Complete: 390/390 bars"
      },
      "time_gaps_1m": {
        "status": "pass",
        "gaps_found": 0,
        "max_gap_minutes": 0,
        "gaps": [],
        "message": "No unexpected gaps"
      },
      "feature_completeness": {
        "status": "pass",
        "bars_total": 390,
        "features_available": 390,
        "coverage_pct": 100.0,
        "message": "Complete: 390/390 bars have features"
      }
    }
  },
  "summary": {
    "total_checks": 20,
    "passed": 18,
    "warnings": 2,
    "errors": 0,
    "critical_errors": 0
  }
}
```

### 使用 jq 分析报告

```bash
# 查看总体状态
cat reports/data_quality_20251027.json | jq '.summary'

# 查看所有 Error 的检查
cat reports/data_quality_20251027.json | \
  jq '.checks[][] | select(.status == "error")'

# 查看特定股票的所有问题
cat reports/data_quality_20251027.json | \
  jq '.checks.AAPL[] | select(.status != "pass")'

# 提取 Critical 问题列表
cat reports/data_quality_20251027.json | \
  jq '.checks[][] | select(.status == "critical") | {check: .message}'
```

---

## 🎯 最佳实践

### 每日流程建议

**盘后 (16:00-17:00 ET)**:
```bash
# 1. 快速检查昨天数据
python tools/comprehensive_data_validation.py \
  --date $(date -v-1d +%Y-%m-%d) \
  --symbols AAPL,TSLA,NVDA,SPY \
  --quick

# 2. 如果有问题，运行完整检查
python tools/comprehensive_data_validation.py \
  --date $(date -v-1d +%Y-%m-%d) \
  --symbols AAPL,TSLA,NVDA,SPY \
  --full \
  --output reports/data_quality_issue_$(date +%Y%m%d).json
```

**每周一次 (周末)**:
```bash
# 完整检查本周所有数据
for day in Mon Tue Wed Thu Fri; do
  date_str=$(date -v-$day +%Y-%m-%d)
  python tools/comprehensive_data_validation.py \
    --date $date_str \
    --symbols AAPL,TSLA,NVDA,GOOGL,MSFT,META,AMZN,SPY,QQQ \
    --full \
    --output reports/weekly/data_quality_$date_str.json
done

# 汇总本周报告
python scripts/summarize_weekly_data_quality.py reports/weekly/
```

---

## 🛠️ 故障排查

### 常见问题

#### Q1: "Table doesn't exist" 错误

```bash
# 检查表是否存在
clickhouse-client --query "SHOW TABLES FROM trading LIKE 'bars_%'"

# 如果表不存在，可能是分辨率错误或数据未写入
# 跳过该检查: --checks completeness,features (不包含 continuity)
```

#### Q2: 特征检查总是失败

```bash
# 检查 bars_1m_current 视图是否正常
clickhouse-client --query "SELECT count() FROM bars_1m_current WHERE toDate(ts_bar_end) = today()"

# 检查 feature calculation 服务是否运行
ps aux | grep feature
```

#### Q3: Kafka lag 检查失败

```bash
# 确保 kafka-consumer-groups.sh 在 PATH 中
which kafka-consumer-groups.sh

# 手动检查
kafka-consumer-groups.sh --bootstrap-server localhost:9092 --all-groups --describe
```

---

## 📚 与其他工具对比

| 工具 | 用途 | 运行时机 | 输出 |
|------|------|---------|------|
| `validate_data.py` | 基础数据检查 | 按需 | 终端输出 |
| `comprehensive_data_validation.py` | **全面数据质量** | **每日盘后** | **JSON + 终端** |
| `system_health_check.py` | 系统健康监控 | 每小时 | JSON |
| `check_ibkr_status.py` | IBKR 连接检查 | 盘前/实时 | 终端输出 |

---

## 📊 报告示例

### 成功案例
```
================================================================================
📊 DATA VALIDATION SUMMARY
================================================================================
Date: 2025-10-27
Symbols: AAPL, TSLA, NVDA

Results:
  Total Checks:     24
  ✅ Passed:        24
  ⚠️  Warnings:      0
  ❌ Errors:        0
  🔴 Critical:      0

  Success Rate: 100.0%
================================================================================

✅ All checks passed successfully
```

### 问题案例
```
================================================================================
📊 DATA VALIDATION SUMMARY
================================================================================
Date: 2025-10-27
Symbols: AAPL, TSLA, NVDA

Results:
  Total Checks:     24
  ✅ Passed:        18
  ⚠️  Warnings:      4
  ❌ Errors:        1
  🔴 Critical:      1

  Success Rate: 75.0%
================================================================================

🔴 CRITICAL ISSUES FOUND - Immediate action required!

Critical Issues:
  - TSLA 1m OHLC consistency: Found 1 inconsistent bar

Errors:
  - NVDA 1m bar completeness: Incomplete: 350/390 bars (89.74%)

Warnings:
  - AAPL time gaps: Found 2 small gaps (max: 3 min)
  - TSLA feature completeness: Mostly complete: 95.5% coverage
```

---

**创建日期**: 2025-10-28
**工具路径**: `./tools/comprehensive_data_validation.py`
**文档路径**: `./docs/daily/DATA_VALIDATION_GUIDE.md`
