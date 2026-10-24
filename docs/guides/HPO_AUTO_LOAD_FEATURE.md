# HPO自动数据加载功能

> **版本**: 2.1
> **日期**: 2025-10-23
> **新功能**: 智能数据完整性检查 + 自动补齐缺失数据

---

## 🎯 功能概述

HPO系统现在会在运行前自动检查数据完整性,如果发现数据缺失,会**自动从IBKR加载**所需数据,无需手动干预。

## ✨ 工作流程

```
1. 解析配置文件
   ↓
2. 检查ClickHouse数据完整性
   ↓
3a. 数据存在 → 继续优化 ✓
   ↓
3b. 数据缺失 → 自动调用 load_hist_multi.py
   ↓
4. 重新检查数据
   ↓
5. 继续优化 ✓
```

## 📝 使用示例

### 默认模式 (自动加载开启)
```bash
venv/bin/python hpo_cli_upgraded.py \
  --config config/hpo_tsla_v7_2.json \
  --study-name my-optimization \
  --runner wf_hpo:evaluate_trial \
  --backend optuna-tpe \
  --n-trials 20 \
  --n-jobs 4 \
  --seed 2025
```

**输出示例**:
```
[HPO] Checking data availability...
[HPO] (Auto-load enabled: will fetch missing data from IBKR)
[HPO]   Checking TSLA...

[HPO] ✗ No data found for TSLA in IS period
[HPO]   Date range: 2024-01-01 09:30:00 to 2024-01-10 16:00:00
[HPO]   Table: trading.features_1m

[HPO] 🔄 Auto-loading missing data...
[HPO]   Symbol: TSLA
[HPO]   Days: 14
[HPO]   Bar size: 1 mins
[HPO]   Running: venv/bin/python tool/load_hist_multi.py --symbols TSLA --days 14 --bar-size 1 mins --fill-mode backfill
[HPO] ✓ Data loading command completed
[HPO]   Inserted 5678 rows to trading.features_1m
[HPO] ✓ Data loaded successfully, re-checking...
[HPO]     ✓ IS: 5678 rows loaded

[HPO] ✓ Data availability check passed
```

### 禁用自动加载
```bash
venv/bin/python hpo_cli_upgraded.py \
  --config config/hpo_tsla_v7_2.json \
  --study-name my-optimization \
  --runner wf_hpo:evaluate_trial \
  --backend optuna-tpe \
  --no-auto-load \
  --n-trials 20
```

**输出示例**:
```
[HPO] Checking data availability...
[HPO]   Checking TSLA...

[HPO] ✗ No data found for TSLA in IS period
[HPO]   Date range: 2024-01-01 09:30:00 to 2024-01-10 16:00:00

[HPO] Suggestions:
[HPO]   1. Check if data exists: clickhouse-client --query "SELECT toDate(ts) as date, count(*) FROM trading.features_1m WHERE symbol='TSLA' GROUP BY date ORDER BY date DESC LIMIT 10"
[HPO]   2. Update config with valid date range
[HPO]   3. Load data: venv/bin/python tool/load_hist_multi.py --symbols TSLA --bar-size 1m --days 30 --fill-mode backfill
Data availability check failed for TSLA
```

## 🔧 技术细节

### 数据检查逻辑
1. **解析配置**: 读取symbols, timeframe, folds
2. **计算时间范围**: 从folds中提取IS和OOS时间段
3. **查询ClickHouse**: 检查每个symbol在每个时间段的数据量
4. **触发加载**: 如果count=0且auto_load=True

### 自动加载参数
- **Symbol**: 从配置文件读取
- **Days**: 自动计算 `(end_date - start_date).days + 5`
- **Bar size**: 从timeframe映射 (1m→"1 mins", 5m→"5 mins", etc.)
- **Fill mode**: 固定使用 `backfill` (完整覆盖时间窗口)

### Timeframe映射表
| 配置 | IBKR barSize |
|------|-------------|
| 1m   | 1 mins      |
| 5m   | 5 mins      |
| 15m  | 15 mins     |
| 1h   | 1 hour      |
| 1d   | 1 day       |

## 🚨 注意事项

### 1. IBKR连接要求
- **TWS/IB Gateway必须运行** - 否则数据加载会失败
- **API连接已配置** - 检查config.yml中的ibkr设置
- **足够的数据订阅** - 确保有权限访问所需symbol的历史数据

### 2. 加载时间
- **1分钟数据**: ~2-5分钟/10天
- **5分钟数据**: ~1-2分钟/30天
- **总超时**: 5分钟,超时会报错

### 3. 网络与限制
- **IBKR pacing限制**: load_hist_multi.py会自动处理分片
- **网络稳定性**: 建议在稳定网络环境下使用
- **磁盘空间**: 确保ClickHouse有足够存储空间

### 4. 数据质量
- **自动去重**: ClickHouse ReplacingMergeTree自动处理重复数据
- **RTH过滤**: 默认只加载常规交易时间数据
- **版本控制**: 使用ver列确保数据一致性

## 📊 优势对比

### 旧方式 (手动)
```bash
# Step 1: 运行HPO,发现数据缺失
venv/bin/python hpo_cli_upgraded.py ...
# ✗ ERROR: No data found for TSLA

# Step 2: 手动加载数据
venv/bin/python tool/load_hist_multi.py --symbols TSLA --days 30 ...

# Step 3: 重新运行HPO
venv/bin/python hpo_cli_upgraded.py ...
```
**耗时**: ~10分钟 (包括手动操作时间)

### 新方式 (自动)
```bash
# 一条命令完成所有
venv/bin/python hpo_cli_upgraded.py ...
# 自动检测 → 自动加载 → 继续优化 ✓
```
**耗时**: ~5分钟 (完全自动化)

## 🎯 适用场景

### ✅ 推荐使用
1. **测试新策略** - 使用新的时间段或symbol
2. **开发环境** - 数据库可能不完整
3. **回测历史** - 需要较早的历史数据
4. **新symbol** - 数据库中没有该ticker数据

### ⚠️ 谨慎使用
1. **生产环境** - 应提前准备好所有数据
2. **高频优化** - 避免重复加载相同数据
3. **云端运行** - 需要VPN或proxy连接到TWS

### ❌ 不推荐
1. **无IBKR连接时** - 使用 `--no-auto-load`
2. **数据已完整时** - 虽然会跳过,但仍检查开销
3. **离线环境** - 无法连接IBKR API

## 🔍 故障排查

### 问题1: "Data loading failed"
**原因**: TWS/IB Gateway未运行或连接失败
**解决**:
```bash
# 检查TWS状态
ps aux | grep -i tws

# 检查配置
cat config/config.yml | grep -A5 ibkr

# 手动测试连接
venv/bin/python tool/load_hist_multi.py --symbols AAPL --days 1 --bar-size 1m
```

### 问题2: "Data loading timed out"
**原因**: 请求的数据量太大(>5分钟)
**解决**:
- 使用 `--no-auto-load` 并手动分批加载
- 或提前准备数据,不依赖auto-load

### 问题3: "Still no data after auto-load"
**原因**: 数据加载成功但写入失败,或时间范围不匹配
**检查**:
```bash
# 查看最近加载的数据
clickhouse-client --query "
  SELECT toDate(ts) as date, count(*)
  FROM trading.features_1m
  WHERE symbol='TSLA'
  GROUP BY date
  ORDER BY date DESC
  LIMIT 10
"
```

## 📚 相关文档
- `tool/load_hist_multi.py` - 数据加载脚本源码
- `HPO_QUICKSTART.md` - HPO快速开始指南
- `HPO_FINAL_STATUS.md` - HPO系统完整状态

---

**更新日志**:
- **2.1** (2025-10-23): 新增自动数据加载功能
- **2.0** (2025-10-23): 添加数据完整性检查
- **1.0** (2025-10-23): 初始HPO系统

**维护者**: Claude Code
**状态**: ✅ 生产就绪
