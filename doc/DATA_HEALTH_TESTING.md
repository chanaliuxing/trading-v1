# ML Pipeline Data Health Testing Framework

## 概述

这是一套**四层分层测试体系**，用于验证 ML 训练管道的数据质量：

```
┌─────────────────────────────────────────────────────┐
│ Layer 4: DSL/特征构建链路                            │
│ → test_dsl_end_to_end.py                            │
│   验证 Brain 提议的 DSL 特征能否正确编译和执行        │
└─────────────────────────────────────────────────────┘
                     ▲
┌─────────────────────────────────────────────────────┐
│ Layer 3: 标签层                                      │
│ → test_label_health.py                              │
│   检查标签覆盖率、正负样本分布、是否全零              │
└─────────────────────────────────────────────────────┘
                     ▲
┌─────────────────────────────────────────────────────┐
│ Layer 2: 特征层                                      │
│ → test_feature_columns_health.py                    │
│   检查特征列是否"空气列"（全NULL、全零、全常数）      │
└─────────────────────────────────────────────────────┘
                     ▲
┌─────────────────────────────────────────────────────┐
│ Layer 1: 原始行情数据                                │
│ → test_raw_bars_health.py                           │
│   验证 1m bars 完整性、缺口检测                       │
└─────────────────────────────────────────────────────┘

                Active表一致性检查
        → test_active_coverage_consistency.py
          检查 features_1m vs features_1m_active 对齐
```

## 核心原则

1. **所有脚本输出 JSON**：便于自动分析和 dashboard 展示
2. **自动跳过当天**：避免 IBKR 当天未完成数据导致误报
3. **统一 ClickHouse 连接**：使用 `libs/db/ch_pool.get_client()`
4. **输出路径统一**：`runs/tests/`

---

## 工具清单

### 1. test_raw_bars_health.py

**作用**：验证原始 1m bar 数据完整性

**用法**：
```bash
venv/bin/python tools/test_raw_bars_health.py \
  --symbols TSLA,SPY,NVDA \
  --start 2025-11-01 \
  --end   2025-11-24 \
  --out runs/tests/raw_bars_health.json
```

**检查项**：
- ✅ 每个交易日理论应有 390 bars (09:30-16:00)
- ✅ 实际 bars 数量和覆盖率
- ✅ 连续缺口检测（默认 >5分钟）

**输出示例**：
```json
{
  "symbol": "TSLA",
  "day": "2025-11-22",
  "expected_bars": 390,
  "actual_bars": 388,
  "coverage": 0.994,
  "max_gap_min": 3
}
```

---

### 2. test_feature_columns_health.py

**作用**：检查特征列是否"空气列"

**用法**：
```bash
venv/bin/python tools/test_feature_columns_health.py \
  --symbols TSLA,NVDA \
  --feature-cols ret_1,ret_5,atr_14,spy_ret_5,vix_z_20 \
  --start 2025-11-01 \
  --end   2025-11-24 \
  --table trading.features_1m \
  --out runs/tests/feature_health.json
```

**检查项**：
- ✅ 总行数 vs 非空行数
- ✅ 非空覆盖率
- ✅ 是否全零（all_zero）
- ✅ 是否全常数（all_constant，distinct values ≤ 2）
- ✅ 数值范围（min, max, mean, std）

**输出示例**：
```json
{
  "symbol": "TSLA",
  "feature": "ret_5",
  "total_rows": 200000,
  "non_null": 198500,
  "non_null_ratio": 0.9925,
  "all_zero": false,
  "all_constant": false,
  "min": -0.045,
  "max": 0.051,
  "mean": 0.0002,
  "std": 0.008
}
```

**质量阈值**：
- ✅ **健康**：`non_null_ratio > 0.9` 且非空气列
- ⚠️  **可疑**：`non_null_ratio 0.5-0.9`
- ❌ **空气列**：`non_null_ratio < 0.1` 或 `all_zero/all_constant`

---

### 3. test_label_health.py

**作用**：验证标签覆盖率和正负样本分布

**用法**：
```bash
venv/bin/python tools/test_label_health.py \
  --symbols TSLA,NVDA \
  --label-cols h5_bps10,h10_bps7 \
  --start 2025-11-01 \
  --end   2025-11-24 \
  --table trading.ml_labels_1m \
  --out runs/tests/label_health.json
```

**检查项**：
- ✅ 非空标签比例
- ✅ 非零标签比例（标签不为 0 的比例）
- ✅ 正样本 vs 负样本分布
- ✅ 样本不平衡检测

**输出示例**：
```json
{
  "symbol": "TSLA",
  "label": "h5_bps10",
  "total_rows": 180000,
  "non_null": 179000,
  "non_zero": 12000,
  "non_zero_ratio": 0.067,
  "pos": 6200,
  "neg": 5800,
  "zero": 167000,
  "pos_ratio": 0.5167,
  "neg_ratio": 0.4833
}
```

**质量阈值**：
- ✅ **健康**：`non_zero_ratio > 0.01` 且正负样本相对平衡
- ⚠️  **稀疏**：`non_zero_ratio < 0.001`
- ❌ **全零**：`non_zero_ratio = 0`

---

### 4. test_active_coverage_consistency.py

**作用**：检查 active 表是否与真实数据对齐

**用法**：
```bash
venv/bin/python tools/test_active_coverage_consistency.py \
  --symbols TSLA,SPY \
  --date 2025-11-24 \
  --min-bars 300 \
  --out runs/tests/active_coverage.json
```

**检查项**：
- ✅ bars_1m_ibkr 行数
- ✅ features_1m 行数和覆盖率
- ✅ features_1m_active 行数和覆盖率
- ✅ 检测 active 表 underfilling（features 高但 active 低）

**输出示例**：
```json
{
  "symbol": "SPY",
  "date": "2025-11-24",
  "bars_count": 390,
  "features_rows": 388,
  "active_rows": 90,
  "features_cov": 0.994,
  "active_cov": 0.231,
  "active_underfilled": true
}
```

**问题诊断**：
- `active_underfilled = true` → 需要运行 `check_active_completeness.py` 或 auto-seed

---

### 5. test_dsl_end_to_end.py

**作用**：测试 Brain 提议的 DSL 特征从编译到执行

**用法**：
```bash
venv/bin/python tools/test_dsl_end_to_end.py \
  --cases-file tests/fixtures/dsl_test_cases.json \
  --sample-symbol TSLA \
  --out runs/tests/dsl_end_to_end.json
```

**输入格式** (`cases.json`):
```json
[
  {
    "name": "ret_5_atr14_ratio",
    "dsl": {"op": "ratio", "num": "f.ret_5", "den": "f.atr_14"},
    "sources": ["f"]
  }
]
```

**检查流程**：
1. DSL → SQL 编译（`dsl_to_sql`）
2. Dry-run 查询 ClickHouse（`dry_run_feature_expr`）
3. 检查结果是否 all-null/all-constant

**输出示例**：
```json
{
  "name": "ret_5_atr14_ratio",
  "compile_ok": true,
  "expr": "(f.ret_5) / nullIf(f.atr_14, 1e-06)",
  "dry_run_ok": true,
  "all_null": false,
  "all_constant": false,
  "error": null
}
```

---

### 6. test_ml_pipeline_health.py

**作用**：综合调用上述所有脚本，生成健康报告

**用法**：
```bash
venv/bin/python tools/test_ml_pipeline_health.py \
  --symbols TSLA,NVDA,SPY \
  --start 2025-11-20 \
  --end   2025-11-24 \
  --feature-cols ret_1,ret_5,atr_14 \
  --label-cols h5_bps10 \
  --out-dir runs/tests/pipeline_health_2025-11-24
```

**生成文件**：
```
runs/tests/pipeline_health_2025-11-24/
├── raw_bars_health.json
├── feature_columns_health.json
├── label_health.json
├── active_coverage.json
└── summary.json
```

**summary.json 示例**：
```json
{
  "timestamp": "2025-11-24T15:30:00",
  "symbols": ["TSLA", "NVDA", "SPY"],
  "summary": {
    "raw_bars_ok": true,
    "features_ok": false,
    "labels_ok": true,
    "active_ok": false
  },
  "issues": [
    "SPY: active underfilled (features=99.4%, active=23.1%)",
    "TSLA.spy_ret_5: air column"
  ]
}
```

---

## 使用场景

### 场景 1：每日数据质量检查

```bash
# 自动化脚本（每日凌晨运行）
venv/bin/python tools/test_ml_pipeline_health.py \
  --symbols TSLA,NVDA,SPY,QQQ,IWM \
  --start $(date -v-5d +%Y-%m-%d) \
  --end   $(date +%Y-%m-%d) \
  --feature-cols ret_1,ret_5,atr_14,vwap,rsi_14 \
  --out-dir runs/tests/daily_$(date +%Y-%m-%d)
```

### 场景 2：发现"空气列"

```bash
# 检查某个特征列是否真的有数据
venv/bin/python tools/test_feature_columns_health.py \
  --symbols SPY \
  --feature-cols spy_ret_5,corr_spy_20,beta_spy_20 \
  --start 2025-11-01 \
  --end   2025-11-24 \
  --out runs/tests/spy_features_check.json
```

### 场景 3：诊断 Active 表覆盖问题

```bash
# 检查 active 表是否正确填充
venv/bin/python tools/test_active_coverage_consistency.py \
  --symbols TSLA,SPY \
  --date 2025-11-22 \
  --out runs/tests/active_check_20251122.json
```

### 场景 4：Brain DSL 回归测试

```bash
# 测试 Brain 最近提议的所有 DSL 特征
venv/bin/python tools/test_dsl_end_to_end.py \
  --cases-file runs/ai_trader/2025-11-24/llm_dsl_cases.json \
  --out runs/tests/dsl_regression.json
```

---

## 质量阈值总结

| 指标 | ✅ 健康 | ⚠️ 警告 | ❌ 失败 |
|------|--------|--------|--------|
| **bars coverage** | >95% | 70-95% | <70% |
| **feature non_null_ratio** | >90% | 50-90% | <50% |
| **feature air column** | false | - | true |
| **label non_zero_ratio** | >1% | 0.1-1% | <0.1% |
| **active coverage** | >90% | 50-90% | <50% |
| **active underfilled** | false | - | true |
| **DSL compile rate** | 100% | 80-100% | <80% |
| **DSL healthy rate** | >80% | 50-80% | <50% |

---

## 故障排查

### 问题 1: bars coverage 低

**症状**：`actual_bars < 300` (77% of 390)

**可能原因**：
1. IBKR 数据断连
2. 数据管道未运行
3. 节假日（美股休市）

**解决方案**：
```bash
# 检查 IBKR 连接状态
python temp/check_ibkr_status.py

# 手动补数据
venv/bin/python scripts/backfill_ibkr_bars.py --symbols TSLA --date 2025-11-22
```

### 问题 2: 特征列全 NULL

**症状**：`non_null_ratio = 0` 或接近 0

**可能原因**：
1. 特征计算逻辑错误
2. 上游依赖列缺失
3. 特征未在 ClickHouse 物化视图中启用

**解决方案**：
```bash
# 检查特征定义
clickhouse-client --query "DESCRIBE TABLE trading.features_1m"

# 检查上游依赖
venv/bin/python tools/test_feature_columns_health.py \
  --symbols TSLA \
  --feature-cols close,open,high,low,volume \
  --start 2025-11-22 \
  --end 2025-11-23
```

### 问题 3: Active 表 underfilled

**症状**：`features_cov = 99%`, `active_cov = 23%`

**原因**：auto-seed 未运行或失败

**解决方案**：
```bash
# 运行 auto-seed
venv/bin/python scripts/check_active_completeness.py \
  --symbols TSLA,SPY \
  --date 2025-11-22 \
  --auto-seed
```

### 问题 4: DSL dry-run 全 NULL

**症状**：`all_null = true`

**可能原因**：
1. 引用的列不存在
2. 数据窗口无数据
3. JOIN 条件错误（m5/d 表）

**解决方案**：
- 检查 schema validation error
- 使用不同的 `--sample-symbol`
- 检查 `sources` 参数是否正确

---

## 自动化集成

### Cron 任务示例

```bash
# /etc/crontab
# 每日凌晨 1:00 运行健康检查
0 1 * * * cd . && venv/bin/python tools/test_ml_pipeline_health.py \
  --symbols TSLA,NVDA,SPY \
  --start $(date -v-7d +\%Y-\%m-\%d) \
  --end $(date +\%Y-\%m-\%d) \
  --out-dir runs/tests/daily_$(date +\%Y-\%m-\%d)
```

### Brain 集成

Brain 可以自动调用这些脚本来验证它提议的特征：

```python
# 伪代码
def brain_propose_feature(dsl_spec):
    # Step 1: 编译测试
    result = test_dsl_compilation(dsl_spec)
    if not result["compile_ok"]:
        return {"status": "rejected", "reason": "compile_failed"}

    # Step 2: Dry-run 测试
    result = test_dsl_dry_run(dsl_spec)
    if result["all_null"]:
        return {"status": "rejected", "reason": "all_null"}

    # Step 3: 小窗口实验
    backfill_feature(dsl_spec, window="3days")
    quality = verify_feature_quality(feature_name)
    if quality["non_null_ratio"] < 0.5:
        return {"status": "rejected", "reason": "low_coverage"}

    # 通过所有检查
    return {"status": "approved"}
```

---

## 参考

- DSL 编译器：`libs/features/feature_dsl_sql.py`
- Brain 工具：`scripts/ai_trader/brain/tools_sql.py`
- ClickHouse 连接池：`libs/db/ch_pool.py`

---

**最后更新**: 2025-11-25
**维护者**: Trading Team
