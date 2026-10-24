# Data Quality Testing - Quick Reference Card

> **速查手册**: 常用测试命令和故障排查

---

## 🚀 快速开始

```bash
# 查看所有命令
make -f Makefile.tests help

# 运行所有测试
make -f Makefile.tests test-all

# 快速冒烟测试 (最快)
make -f Makefile.tests test-quick

# 提交前检查
make -f Makefile.tests test-pre-commit
```

---

## 📊 分层测试命令

### 数据层 (Data Layer)

```bash
# Test A: Bars Coverage
venv/bin/python tools/test_bars_coverage.py \
  --symbols TSLA,SPY --start 2025-11-24 --end 2025-11-24

# Test C: Features Coverage
venv/bin/python tools/test_features_coverage.py \
  --date 2025-11-24 --symbols TSLA,SPY
```

### 特征层 (Feature Layer)

```bash
# Test D: Feature Quality
pytest tests/integration/features/test_feature_quality.py -v

# Test E: DSL Compilation
pytest tests/unit/features/test_feature_dsl_sql_llm_cases.py -v
```

### 标签层 (Label Layer)

```bash
# Test F: Label Sanity
venv/bin/python tools/test_label_sanity.py \
  --date 2025-11-24 --symbols TSLA,SPY

# Test G: Label Alignment
venv/bin/python tools/test_label_feature_alignment.py \
  --date 2025-11-24 --symbols TSLA,SPY
```

### 端到端 (End-to-End)

```bash
# Test B: DataHealer
pytest tests/integration/data/test_data_healer_smoke.py -v

# Test H: E2E Pipeline
pytest tests/integration/pipeline/test_ml_pipeline_smoke.py -v
```

---

## 🔧 故障排查

### ClickHouse 连接失败

```bash
# 检查服务
docker ps | grep clickhouse

# 启动服务
cd infra && docker-compose up -d clickhouse

# 测试连接
venv/bin/python -c "from libs.db.ch_pool import get_client; print(get_client().query('SELECT 1'))"
```

### Test 失败 - Bars 缺失

```bash
# 手动回填 bars
venv/bin/python tool/load_hist_multi.py \
  --symbols TSLA --days 5 --bar-size 1m --fill-mode backfill
```

### Test 失败 - Features 缺失

```bash
# 重建 features
venv/bin/python pipelines/rebuild_features_1m.py \
  --symbol TSLA --start 2025-11-20 --end 2025-11-24
```

### Test 失败 - Labels 表不存在

```bash
# 生成 labels
venv/bin/python pipelines/generate_labels.py \
  --symbols TSLA SPY --horizons 5 15 30
```

---

## 📈 常用场景

### 每日巡检

```bash
# 方式 1: Makefile
make -f Makefile.tests test-data-layer

# 方式 2: Crontab
0 22 * * * cd /path/to/trading && make -f Makefile.tests test-data-layer
```

### 训练前检查

```bash
make -f Makefile.tests test-feature-layer
make -f Makefile.tests test-label-layer
```

### 部署后验证

```bash
make -f Makefile.tests test-healing-e2e
```

### 测试特定日期

```bash
make -f Makefile.tests test-date DATE=2025-11-20 SYMBOLS=TSLA,SPY,NVDA
```

---

## 📊 Pass 标准速查

| Test | Pass 标准 |
|------|----------|
| A | ≥380 bars/天 |
| B | 11/11 测试通过 |
| C | Features/Bars ≥98% |
| D | 非空率≥90%, std>0 |
| E | 21/21 测试通过 |
| F | 非空率≥95%, 无极端值 |
| G | Orphan率≤1% |
| H | 流程无异常, 预测合理 |

---

## 🔍 报告查看

```bash
# 生成报告
make -f Makefile.tests test-all

# 查看报告摘要
make -f Makefile.tests report

# 报告位置
ls -lh reports/$(date +%Y%m%d)_*/
```

---

## 🚨 紧急情况

### 所有测试失败

```bash
# 1. 检查基础设施
docker ps
make status

# 2. 检查 ClickHouse
docker logs clickhouse

# 3. 重启服务
cd infra && docker-compose restart clickhouse

# 4. 重新运行测试
make -f Makefile.tests test-quick
```

### CI/CD 失败

1. 查看 GitHub Actions 日志
2. 下载 Artifacts 查看详细报告
3. 本地复现: `make -f Makefile.tests test-all`
4. 修复后推送

---

## 📞 获取帮助

```bash
# 查看测试帮助
make -f Makefile.tests help

# 查看详细文档
cat tests/README_DATA_QUALITY.md

# 查看集成报告
cat DATA_QUALITY_INTEGRATION_COMPLETE.md
```

---

**维护者**: Trading Team
**更新**: 2025-11-25
