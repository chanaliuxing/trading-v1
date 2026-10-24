# Data Quality Testing Framework

> **完整的数据质量保障体系，覆盖数据层 → 特征层 → 标签层 → 端到端**

## 📋 概览

本测试框架实现了 8 个测试，形成完整的质量保障矩阵：

```
数据层 → 特征层 → 标签层 → 端到端
  ↓        ↓        ↓        ↓
Test A   Test C   Test F   Test H
Test B   Test D   Test G
         Test E
```

### 测试矩阵

| ID | 测试名称 | 类型 | 用途 | 关键指标 |
|----|---------|------|------|---------|
| **A** | Bars Coverage | CLI Tool | 验证 RTH bars 覆盖率 | ≥380 bars/天 |
| **B** | DataHealer Smoke | Pytest | 数据自愈功能验证 | 检测+回填+重试 |
| **C** | Features Coverage | CLI Tool | Features vs Bars 对齐 | ≥98% 覆盖率 |
| **D** | Feature Quality | Pytest | 特征质量检查 | 非空率≥90%, std>0 |
| **E** | DSL Compilation | Pytest | DSL→SQL 编译验证 | 18 test cases |
| **F** | Label Sanity | CLI Tool | Label 取值合理性 | 非空率≥95%, 无极端值 |
| **G** | Label Alignment | CLI Tool | Label-Feature 时间对齐 | Orphan率≤1% |
| **H** | E2E Pipeline | Pytest | ML Pipeline 冒烟测试 | 完整流程无异常 |

---

## 🚀 快速开始

### 1. 本地运行 (使用 Makefile)

```bash
# 查看所有可用命令
make -f Makefile.tests help

# 运行所有测试
make -f Makefile.tests test-all

# 只运行数据层测试
make -f Makefile.tests test-data-layer

# 快速冒烟测试 (开发时使用)
make -f Makefile.tests test-quick

# 测试特定日期
make -f Makefile.tests test-date DATE=2025-11-24 SYMBOLS=TSLA,SPY

# 查看测试报告
make -f Makefile.tests report

# 清理报告
make -f Makefile.tests clean
```

### 2. 手动运行单个测试

#### 数据层测试

```bash
# Test A: Bars Coverage
venv/bin/python tools/test_bars_coverage.py \
  --symbols TSLA,SPY,NVDA \
  --start 2025-11-20 \
  --end 2025-11-24 \
  --output reports/test_a.json

# Test C: Features Coverage
venv/bin/python tools/test_features_coverage.py \
  --date 2025-11-24 \
  --symbols TSLA,SPY,NVDA \
  --threshold 0.98 \
  --output reports/test_c.json
```

#### 特征层测试

```bash
# Test D: Feature Quality (pytest)
venv/bin/pytest tests/integration/features/test_feature_quality.py -v

# Test E: DSL Compilation (pytest)
venv/bin/pytest tests/unit/features/test_feature_dsl_sql_llm_cases.py -v
```

#### 标签层测试

```bash
# Test F: Label Sanity
venv/bin/python tools/test_label_sanity.py \
  --date 2025-11-24 \
  --symbols TSLA,SPY \
  --output reports/test_f.json

# Test G: Label-Feature Alignment
venv/bin/python tools/test_label_feature_alignment.py \
  --date 2025-11-24 \
  --symbols TSLA,SPY \
  --output reports/test_g.json
```

#### 数据自愈 & 端到端

```bash
# Test B: DataHealer Smoke (pytest)
venv/bin/pytest tests/integration/data/test_data_healer_smoke.py -v

# Test H: End-to-End Pipeline (pytest)
venv/bin/pytest tests/integration/pipeline/test_ml_pipeline_smoke.py -v
```

---

## 📊 CI/CD 集成

### GitHub Actions

已集成到 `.github/workflows/data-quality-tests.yml`

**触发条件：**
- ✅ Push to `main` / `dev` 分支
- ✅ Pull Request to `main` / `dev`
- ✅ 每日定时运行 (UTC 14:00 / 北京时间 22:00)
- ✅ 手动触发 (`workflow_dispatch`)

**工作流阶段：**

```
Stage 1: Data Layer Tests (A, C)
   ↓
Stage 2: Feature Layer Tests (D, E)
   ↓
Stage 3: Label Layer Tests (F, G)
   ↓
Stage 4: Healing & E2E (B, H)
   ↓
Stage 5: Generate Summary & Artifacts
```

**查看结果：**
1. GitHub Actions 页面查看实时日志
2. 下载 Artifacts 查看详细报告（JSON/HTML/JUnit XML）
3. PR 自动评论显示测试结果摘要

### 本地持续监控

```bash
# 每小时自动运行数据层测试
make -f Makefile.tests monitor
```

---

## 📁 文件结构

```
trading/
├── .github/workflows/
│   └── data-quality-tests.yml       # CI/CD 配置
├── Makefile.tests                    # 本地测试 Makefile
├── tools/
│   ├── test_bars_coverage.py        # Test A
│   ├── test_features_coverage.py    # Test C
│   ├── test_label_sanity.py         # Test F
│   └── test_label_feature_alignment.py # Test G
├── tests/
│   ├── integration/
│   │   ├── data/
│   │   │   └── test_data_healer_smoke.py      # Test B
│   │   ├── features/
│   │   │   └── test_feature_quality.py        # Test D
│   │   └── pipeline/
│   │       └── test_ml_pipeline_smoke.py      # Test H
│   └── unit/
│       └── features/
│           ├── test_dsl_cases.json            # Test E cases
│           └── test_feature_dsl_sql_llm_cases.py # Test E
└── reports/                          # 测试报告输出目录
```

---

## 🔍 测试详解

### Test A: Bars Coverage

**目的**: 验证 `trading.bars_1m_ibkr` 在 RTH (09:30-16:00) 有完整覆盖

**Pass 标准**:
- 每个交易日 ≥380 bars (理想值 390)
- 无大面积缺口

**失败原因**:
- DataHealer 未运行
- IBKR 数据源断连
- `load_hist_multi.py` 回填失败

**修复建议**:
```bash
# 手动回填
venv/bin/python tool/load_hist_multi.py \
  --symbols TSLA \
  --days 5 \
  --bar-size 1m \
  --fill-mode backfill
```

---

### Test B: DataHealer Smoke

**目的**: 验证数据自愈功能正常工作

**测试场景**:
1. 检测数据缺失 ✅
2. 触发回填脚本 ✅
3. 重建 features_1m ✅
4. 处理回填失败并重试 ✅
5. 周末场景不误判 ✅

**Pass 标准**: 11/11 测试通过

**Note**: 使用 mock 避免实际回填，测试用时 ~4 分钟

---

### Test C: Features Coverage

**目的**: 验证 `features_1m` 与 `bars_1m_ibkr` 行数对齐

**Pass 标准**:
- Features count / Bars count ≥ 98%

**失败原因**:
- `rebuild_features_1m.py` 未运行
- ReplacingMergeTree 去重问题 (需使用 `FINAL`)

**修复建议**:
```bash
# 重建 features
venv/bin/python pipelines/rebuild_features_1m.py \
  --symbol TSLA \
  --start 2025-11-20 \
  --end 2025-11-24
```

---

### Test D: Feature Quality

**目的**: 验证特征质量（非空率、统计分布）

**特征组**:
- `core_enhanced`: ret_5m, vol_5m, realized_vol_5m, vwap_dist_5m, volume_rank_20
- `cross_asset`: corr_spy_20, beta_spy_20, ret_vs_spy_5m
- `vix`: vix_z_20 (允许 ≥50% 覆盖率)

**Pass 标准**:
- 非空率 ≥ 90% (VIX: ≥50%)
- 非全零、非全常数
- 标准差 > 0

**失败原因**:
- 特征计算公式错误
- 依赖数据缺失 (如 SPY 数据缺失导致跨资产特征为空)

---

### Test E: DSL Compilation

**目的**: 验证 Feature DSL → ClickHouse SQL 编译正确性

**测试用例**: 18 cases (11 成功场景 + 7 失败场景)

**支持的操作**:
- `ratio`: num / nullif(den, eps)
- `ratio_minus_1`: ratio - 1
- `log1p`: log1p(col)
- `product`: col1 * col2
- `sum`: col1 + col2
- `abs`: abs(col)

**Pass 标准**: 21/21 测试通过 (包含 3 个汇总测试)

---

### Test F: Label Sanity

**目的**: 验证 label 取值合理性

**检查项**:
1. 非空率 ≥ 95%
2. 均值在 [-0.05, +0.05] (±5% 日内波动)
3. 标准差 > 0
4. 无极端值 (|label| < 0.5)

**失败原因**:
- Label 生成逻辑错误
- 全为 NULL (数据不足无法计算未来收益)
- 极端市场波动

---

### Test G: Label-Feature Alignment

**目的**: 验证 label 表 `ts_features` 与 `features_1m.ts` 严格对齐

**Pass 标准**:
- Orphan 时间戳 ≤ 1%

**失败原因**:
- Label 生成使用了不存在的时间戳
- Features 表数据不完整

**Note**: 如果系统中 label 与 feature 存储在同一表 (`features_1m_with_labels`)，此测试自动通过

---

### Test H: End-to-End Pipeline

**目的**: 验证完整 ML 流程正常工作

**流程**:
1. 从 ClickHouse 读取 features_1m ✅
2. 生成 labels (horizon=5m) ✅
3. 训练 LGBMRegressor (3 trees) ✅
4. 验证预测值合理性 ✅

**Pass 标准**:
- Labels 非空率 ≥ 95%
- 模型训练无异常
- 预测值无 NaN/Inf
- 预测值 |y_pred| < 0.5

**失败原因**:
- ClickHouse 无最近数据
- LightGBM 未安装
- 特征全为 NULL

---

## 🔧 故障排查

### 常见问题

#### 1. ClickHouse 连接失败

```bash
# 检查 ClickHouse 是否运行
docker ps | grep clickhouse

# 启动 ClickHouse
cd infra && docker-compose up -d clickhouse

# 测试连接
clickhouse-client --query "SELECT 1"
```

#### 2. Test 报告找不到

```bash
# 检查报告目录
ls -lh reports/

# 使用 Makefile 生成报告
make -f Makefile.tests test-all
make -f Makefile.tests report
```

#### 3. Pytest 导入错误

```bash
# 重新安装依赖
venv/bin/pip install -r requirements.txt
venv/bin/pip install pytest pytest-cov pytest-html
```

#### 4. Label 表不存在

Tests F 和 G 会优雅处理此情况（`continue-on-error: true`），因为 label 表可能尚未生成。

如需生成 label 表：
```bash
venv/bin/python pipelines/generate_labels.py \
  --symbols TSLA SPY \
  --horizons 5 15 30
```

---

## 📈 最佳实践

### 开发阶段

```bash
# 1. 修改代码前运行快速测试
make -f Makefile.tests test-quick

# 2. 提交前运行 pre-commit 测试
make -f Makefile.tests test-pre-commit

# 3. Push 前运行完整测试
make -f Makefile.tests test-all
```

### 生产环境

```bash
# 1. 每日巡检 (crontab)
0 22 * * * cd /path/to/trading && make -f Makefile.tests test-data-layer

# 2. 训练前检查
make -f Makefile.tests test-feature-layer
make -f Makefile.tests test-label-layer

# 3. 部署后验证
make -f Makefile.tests test-healing-e2e
```

### CI/CD 配置

```yaml
# .gitlab-ci.yml 示例
data-quality-tests:
  stage: test
  script:
    - make -f Makefile.tests test-all
  artifacts:
    reports:
      junit: reports/*_junit.xml
    paths:
      - reports/
    expire_in: 30 days
  only:
    - main
    - dev
```

---

## 🎯 性能基准

| 测试 | 预期耗时 | 数据量 |
|------|---------|--------|
| Test A | <10s | 1天×3 symbols |
| Test B | ~4min | Mock (无实际数据) |
| Test C | <10s | 1天×3 symbols |
| Test D | <30s | 5天×2 symbols |
| Test E | <5s | 18 test cases |
| Test F | <10s | 1天×2 symbols |
| Test G | <10s | 1天×2 symbols |
| Test H | <30s | 1天×2 symbols |

**总耗时**: ~6 分钟 (并行执行可缩短到 ~5 分钟)

---

## 📞 支持

- **问题反馈**: GitHub Issues
- **文档**: `/docs/testing/`
- **示例**: `tests/integration/features/test_feature_quality.py`

---

## 📝 更新日志

### v1.0.0 (2025-11-25)

- ✅ 实现完整测试矩阵 (Tests A-H)
- ✅ GitHub Actions CI/CD 集成
- ✅ Makefile 本地测试工具
- ✅ 8 个测试全部验证通过

---

**维护者**: Trading Team
**最后更新**: 2025-11-25
