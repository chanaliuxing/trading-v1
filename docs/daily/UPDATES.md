# 每日运维工具更新日志

## 2025-10-28 (2) - 全面数据质量检查工具 🆕

### ✨ 新增功能

#### **comprehensive_data_validation.py** - 全面数据质量验证

**核心特性**:
- 🔍 **5大检查类别**, 10+ 检查项目
- 📊 **多分辨率检查** - 支持 1m/5m/15m/1h/1d
- 🎯 **智能阈值** - Pass/Warning/Error/Critical 分级
- 📄 **JSON 报告** - 结构化输出，便于自动化
- ⏰ **历史追溯** - 可检查任意历史日期

**检查类别**:

1. **Completeness（完整性）**:
   - Bar 数据完整性（多分辨率）
   - Tick 数据量验证
   - 预期 vs 实际对比

2. **Continuity（连续性）**:
   - 时间序列间隔检查
   - 重复时间戳检测
   - Gap 分析与定位

3. **Features（特征验证）**:
   - 特征计算覆盖率
   - EMA/指标准确性验证
   - Features 查漏补缺

4. **Accuracy（准确性）**:
   - OHLC 逻辑一致性
   - 价格异常波动检测（>10%）
   - 数据合理性验证

5. **Infrastructure（基础设施）**:
   - Kafka Consumer LAG 监控
   - 数据处理延迟检测

**使用方法**:
```bash
# 快速检查（推荐每日使用）
./scripts/daily_data_check.sh

# 指定日期
./scripts/daily_data_check.sh 2025-10-27

# 完整验证
./scripts/daily_data_check.sh --full

# 自定义检查
python tools/comprehensive_data_validation.py \
  --date 2025-10-27 \
  --symbols AAPL,TSLA \
  --checks continuity,features \
  --output reports/data_quality.json
```

**与原 validate_data.py 对比**:
```
✅ 数据完整性:    基础检查 → 多分辨率、多维度检查
✅ NULL 值:       支持 → 支持
✅ 离群值:        基础统计 → 智能价格波动检测
✅ 数据新鲜度:     支持 → 支持
🆕 数据连续性:     ❌ → ✅ 时间间隔、重复检测
🆕 特征完整性:     ❌ → ✅ Coverage 检查
🆕 特征准确性:     ❌ → ✅ 指标合理性验证
🆕 OHLC 一致性:   ❌ → ✅ 逻辑检查
🆕 价格异常:      ❌ → ✅ >10% 波动检测
🆕 Tick 数据量:   ❌ → ✅ Trades/Quotes 验证
🆕 Kafka 延迟:    ❌ → ✅ LAG 监控
🆕 JSON 报告:     ❌ → ✅ 结构化输出
```

### 📁 新增文件

1. **tools/comprehensive_data_validation.py** (600+ lines)
   - 主要检查脚本
   - 10+ 检查函数
   - JSON 报告生成
   - 智能阈值判断

2. **docs/daily/DATA_VALIDATION_GUIDE.md** (600+ lines)
   - 完整使用指南
   - 每个检查项的详细说明
   - 最佳实践
   - 故障排查
   - JSON 格式说明

3. **scripts/daily_data_check.sh** (50 lines)
   - 快捷检查脚本
   - 支持快速/完整模式
   - 自动生成报告

### 🔧 集成建议

**每日盘后**（`evening_review.sh`）:
```bash
# 添加到 evening_review.sh
echo "=========================================="
echo "Data Quality Validation"
echo "=========================================="
./scripts/daily_data_check.sh --full
```

**定时任务**（Cron）:
```bash
# 每天盘后 5PM 自动检查
0 17 * * 1-5 cd . && ./scripts/daily_data_check.sh --full >> logs/data_validation.log 2>&1
```

**告警集成**:
```bash
# 检查退出码，发送告警
if ! ./scripts/daily_data_check.sh; then
    # Send alert to Slack/Email
    echo "Data quality issues detected!" | mail -s "Alert" user@example.com
fi
```

### 📊 报告格式

**JSON 输出结构**:
```json
{
  "timestamp": "2025-10-28T17:00:00",
  "date": "2025-10-27",
  "symbols": ["AAPL", "TSLA"],
  "checks": {
    "AAPL": {
      "bar_completeness_1m": {
        "status": "pass",
        "expected": 390,
        "actual": 390,
        "completeness_pct": 100.0
      },
      "time_gaps_1m": {
        "status": "pass",
        "gaps_found": 0
      },
      "feature_completeness": {
        "status": "pass",
        "coverage_pct": 100.0
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

**查询报告**:
```bash
# 查看总体状态
cat reports/data_quality_20251027.json | jq '.summary'

# 查看所有错误
cat reports/data_quality_20251027.json | jq '.checks[][] | select(.status == "error")'

# 查看特定股票
cat reports/data_quality_20251027.json | jq '.checks.AAPL'
```

---

## 2025-10-28 (1) - 新增系统健康检查脚本 🆕

### ✨ 新增功能

#### **system_health_check.py** - 自动化系统监测

**核心特性**:
- 🔍 自动执行 7 大方面检查
- 📄 生成结构化 JSON 报告
- 🤖 固定文件名（`system_health.json`），方便 AI 读取
- ⏰ 包含时间戳，可追溯历史
- 📊 历史记录自动保存到 `health_history/` 目录

**检查项目**:
1. **Docker 容器状态** - 所有容器运行状态和健康检查
2. **run_system.py** - 进程状态和最近错误数量
3. **IBKR 连接** - 连接状态验证
4. **数据质量** - Tick 数据和 Bar 数据完整性
5. **Kafka Consumers** - Consumer Groups LAG 统计
6. **错误日志** - ERROR 和 CRITICAL 统计
7. **系统资源** - CPU、内存、磁盘使用率

**使用方法**:
```bash
cd ./docs/daily
./system_health_check.py          # 基本检查
./system_health_check.py -v       # 显示详细日志
```

**报告位置**:
- 最新报告: `./logs/system_health.json`
- 历史记录: `./logs/health_history/system_health_YYYYMMDD_HHMMSS.json`

**状态码**:
- `0` - 系统健康
- `2` - 有警告
- `1` - 有错误

**AI 集成示例**:
```python
import json

with open("./logs/system_health.json") as f:
    report = json.load(f)

print(f"检查时间: {report['timestamp_readable']}")
print(f"总体状态: {report['overall_status']}")
print(f"检查通过: {report['summary']['passed']}/{report['summary']['total_checks']}")
```

### 📁 新增文件

1. **system_health_check.py** (19 KB)
   - 主要检查脚本

2. **HEALTH_CHECK_GUIDE.md** (12 KB)
   - 完整使用指南
   - JSON 格式说明
   - AI 读取示例
   - 故障排查

3. **install_dependencies.sh** (789 B)
   - 依赖安装脚本（psutil）

### 🔧 依赖要求

新增依赖：
```bash
pip install psutil
```

或运行安装脚本：
```bash
./install_dependencies.sh
```

### 📊 使用建议

**每日运维**:
- 盘前：运行健康检查，确认系统正常
- 盘中：通过 cron 每小时自动检查
- 盘后：生成报告，供 AI 分析

**定时任务示例**:
```bash
# 每小时执行
0 * * * * ./docs/daily/system_health_check.py >> ./logs/health_check.log 2>&1
```

---

## 2025-10-27 - 整合 000_important_command.md 中的命令

### ✅ 已添加的重要命令

#### 1. **LLM Trader 详细命令**

**盘前交易** (04:00-09:30):
```bash
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --interval 600 \
  --cycles 33 \
  --close-at "09:35" \
  --forbid-new-positions-before 9 \
  --wait-for-premarket
```

**盘中交易** (09:30-16:00):
```bash
# 开盘第一小时快速交易
python run_single_bot.py \
  --provider deepseek \
  --symbols TSLA \
  --interval 60 \
  --wait-for-market \
  --start-trading-at "9:40" \
  --stop-trading-at "10:30" \
  --close-at "10:45"

# 全天日内交易
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols AAPL NVDA \
  --interval 600 \
  --cycles 33 \
  --close-at "15:59" \
  --wait-for-market
```

**盘后交易** (16:00-20:00):
```bash
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --wait-for-afterhours
```

**隔夜交易** (20:00-04:00):
```bash
python run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --interval 600 \
  --cycles 50 \
  --wait-for-overnight \
  --close-at "03:50" \
  --forbid-new-positions-before 5
```

#### 2. **批量加载历史数据**

```bash
# 单个股票加载 200 个交易日
python tool/load_hist_multi.py \
  --symbols AAPL \
  --bar-size 1m \
  --sessions 200 \
  --fill-mode backfill \
  --chunk-days 7

# 多个股票
python tool/load_hist_multi.py \
  --symbols AAPL TSLA NVDA GOOGL MSFT META AMZN \
  --bar-size 1m \
  --sessions 100 \
  --fill-mode backfill \
  --chunk-days 7
```

#### 3. **Google News 抓取**

```bash
python ./llm_trader/scripts/googleNews.py
```

#### 4. **Alpha Loop 协作优化**

```bash
# Optuna + LLM + ML 协作优化
venv/bin/python alpha_loop.py \
  --config alpha_config_tsla_robust.json \
  --provider coop_optuna \
  --gens 2 \
  --out_dir alpha_out/$(date +%Y%m%d) \
  --k-top 10

# 查看结果
cat alpha_out/$(date +%Y%m%d)/best_params.json
cat alpha_out/$(date +%Y%m%d)/gen_01/llm_refine.json
cat alpha_out/$(date +%Y%m%d)/gen_01/coop_merged.json
```

#### 5. **ClickHouse 备份**

```bash
cd .
make recon-all

# 备份保存位置: backup/clickhouse/
# 格式: full_backup_YYYYMMDD_HHMMSS
```

#### 6. **PostgreSQL 备份**

```bash
pg_dump -h localhost -U postgres -d test > backup/pg/manual_$(date +%Y%m%d_%H%M%S).sql
```

---

### 📁 更新的文件

1. **QUICK_REFERENCE.md** - 添加：
   - LLM Trader 各时段详细命令
   - 批量加载历史数据
   - Google News 抓取
   - Alpha Loop 协作优化
   - 备份命令

2. **DAILY_OPERATIONS_CHECKLIST.md** - 添加：
   - 盘前 Google News 抓取步骤
   - 盘前/盘中/盘后/隔夜 LLM Trader 详细命令
   - 批量加载历史数据说明
   - 备份与恢复完整流程

---

### 🔍 命令来源

所有命令来自 `./000_important_command.md`

**已整合的命令**:
- ✅ LLM Trader 各时段运行命令
- ✅ 批量加载历史数据
- ✅ Google News 抓取
- ✅ Alpha Loop 协作优化
- ✅ ClickHouse/PostgreSQL 备份

**未整合的命令** (不相关或已包含):
- ❌ Git 相关命令（不是每日运维）
- ❌ `docker-compose up -d`（已在其他部分）
- ❌ `codex` 相关（开发工具，非运维）

---

### 📊 关键改进

#### 1. **LLM Trader 更灵活**
- 支持各时段精确控制（盘前/盘中/盘后/隔夜）
- 支持时间窗口设置（start-trading-at, stop-trading-at）
- 支持风控设置（forbid-new-positions-before, close-at）

#### 2. **数据管理更完善**
- 批量加载历史数据（首次设置）
- 每日数据回补（增量更新）
- 新闻数据抓取（辅助分析）

#### 3. **优化流程更智能**
- 协作优化（Optuna + LLM + ML）
- 自动化流程（1小时全自动）
- 结果可追踪（JSON 文件保存）

#### 4. **备份更可靠**
- ClickHouse 完整备份
- PostgreSQL 增量备份
- 每日自动运行

---

### 🎯 使用建议

#### 每日必做：
1. 盘前：运行 `morning_check.sh`
2. 启动 LLM Trader（选择合适的时段命令）
3. 盘后：运行 `evening_review.sh`

#### 每周必做：
1. 运行 Alpha Loop 优化参数
2. 检查备份文件完整性
3. 清理旧日志

#### 按需使用：
1. 批量加载历史数据（首次设置或大量补数据）
2. Google News 抓取（盘前分析参考）
3. 手动备份（重大变更前）

---

**更新时间**: 2025-10-27 23:40
**更新内容**: 整合 000_important_command.md 中的所有相关命令
**影响文件**: QUICK_REFERENCE.md, DAILY_OPERATIONS_CHECKLIST.md
