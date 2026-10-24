# 数据质量监控指南

## 概述

本文档介绍两层数据质量监控工具:

1. **基础监控** (`tools/data_quality_monitor.py`) - 快速日常检查
2. **高级监控** (`tools/data_quality_advanced.py`) - 深度ML准备验证

## 一、基础监控 (Quick Health Check)

### 用途
- 每日 cron 任务
- 快速检查 bars/features/labels 覆盖率
- 检测异常 null 比率和 bar 计数

### 使用方法

```bash
# 检查最近7天数据质量
python tools/data_quality_monitor.py --symbols TSLA NVDA --days 7

# 输出示例
✅ Data quality OK (bars/labels)

# 或者发现问题时
⚠️  Data quality warnings:
  [bars] TSLA 2025-11-28 bars=195  # 应为390
  [labels] NVDA null_ratio=0.6 total=2422  # 标签缺失过多
```

### 检查项
- **Bars**: 每日 RTH 期间是否有 ~390 条1分钟K线 (6.5h × 60min)
- **Labels**: `label_h30` null 比率 < 50%

## 二、高级监控 (Advanced ML Readiness)

### 用途
- **训练前必检** - 在启动 OOF/HPO 前验证数据质量
- **定位 AUC≈0.5 根因** - 检测特征漂移、标签泄露、时间对齐问题

### 核心检查项

#### 1. 特征分布漂移检测 (Feature Drift)

**问题**: Active vs Baseline 表特征分布不一致,导致模型在新数据上失效

**检测方法**:
- Kolmogorov-Smirnov 双样本检验
- 比较均值/标准差偏移百分比
- 默认阈值: p-value < 0.05 则判定为漂移

**使用**:
```bash
python tools/data_quality_advanced.py \
  --symbols TSLA NVDA \
  --lookback-days 30 \
  --mode drift-only
```

**输出解读**:
```json
{
  "check": "feature_drift",
  "passed": false,
  "by_symbol": {
    "TSLA": {
      "rsi": {
        "status": "drifted",
        "ks_statistic": 0.1523,
        "p_value": 0.0023,  // < 0.05, 判定为漂移
        "active_mean": 52.3,
        "baseline_mean": 48.1,
        "mean_shift_pct": 8.73,  // 均值偏移8.73%
        "std_shift_pct": 12.45
      }
    }
  },
  "summary": {
    "drifted_features": ["TSLA:rsi", "NVDA:macd"]
  }
}
```

**修复措施**:
1. 重新计算 baseline 表: `python tools/update_features_baseline.py`
2. 检查特征计算逻辑是否变更
3. 确认原始数据源无异常

---

#### 2. 标签泄露检测 (Label Leakage)

**问题**: `label_h30` 可能使用了未来数据,导致训练时"作弊",但实盘无法复现

**检测方法**:
- 验证标签与未来收益率的相关性 > 与当前价格的相关性
- 检查标签是否意外依赖当前 bar 的 close 价格

**使用**:
```bash
python tools/data_quality_advanced.py \
  --symbols TSLA \
  --horizon-min 30 \
  --mode leakage-only
```

**输出解读**:
```json
{
  "check": "label_leakage",
  "passed": false,
  "by_symbol": {
    "TSLA": {
      "status": "leakage_detected",
      "label_future_corr": 0.12,   // 与30分钟后收益率相关性
      "label_current_corr": 0.67,  // ❌ 与当前价格相关性过高!
      "warning": "Label should correlate with future returns, not current price"
    }
  }
}
```

**修复措施**:
1. 检查 `tools/recompute_labels_1m.py` 逻辑
2. 确认标签计算只使用 `ts + horizon` 之后的数据
3. 验证回测无 forward-looking bias

---

#### 3. 数据对齐验证 (Data Alignment)

**问题**: 特征与标签时间戳不匹配,或存在大量时间间隙

**检测方法**:
- 检测 >5分钟的时间间隙 (应为1分钟连续数据)
- 验证标签覆盖率 > 50%

**使用**:
```bash
python tools/data_quality_advanced.py \
  --symbols TSLA NVDA \
  --mode alignment-only
```

**输出解读**:
```json
{
  "check": "data_alignment",
  "passed": false,
  "by_symbol": {
    "TSLA": {
      "status": "misaligned",
      "total_bars": 2422,
      "large_gaps_count": 145,      // 145次 >5分钟间隔
      "large_gaps_pct": 6.0,        // ❌ > 5%阈值
      "max_gap_minutes": 120,
      "label_coverage": 0.83,       // ✅ 标签覆盖率正常
      "issues": ["excessive_timestamp_gaps"]
    }
  }
}
```

**修复措施**:
1. 运行数据回填: `python tools/backfill_bars.py --symbols TSLA --start 2025-11-01 --end 2025-11-28`
2. 检查 IBKR 连接中断记录
3. 验证 `bars_1m_ibkr` 表完整性

---

## 三、综合使用流程

### 场景1: 日常数据健康监控

```bash
# 每日4:30PM ET (市场收盘后30分钟) 运行 cron
30 16 * * 1-5 cd . && \
  venv/bin/python tools/data_quality_monitor.py --symbols TSLA NVDA --days 1 >> logs/dq_daily.log
```

### 场景2: 训练前验证 (预防 AUC≈0.5)

```bash
# Step 1: 快速检查覆盖率
python tools/data_quality_monitor.py --symbols TSLA NVDA --days 30

# Step 2: 如果快速检查通过,运行深度检查
python tools/data_quality_advanced.py \
  --symbols TSLA NVDA \
  --lookback-days 30 \
  --horizon-min 30 \
  --mode all \
  --output reports/dq_$(date +%Y%m%d).json

# Step 3: 如果全部通过,启动训练
python scripts/ai_trader/nightly_oof.py --symbols TSLA NVDA
```

### 场景3: 定位 AUC≈0.5 根因

**症状**: 日志中所有模型 AUC=0.5, IC=null

**诊断步骤**:

```bash
# 1. 检查数据对齐
python tools/data_quality_advanced.py --mode alignment-only --symbols TSLA

# 2. 检查标签泄露
python tools/data_quality_advanced.py --mode leakage-only --symbols TSLA

# 3. 检查特征漂移
python tools/data_quality_advanced.py --mode drift-only --symbols TSLA

# 4. 运行 preflight 检查
python scripts/ai_trader/brain/preflight.py --symbols TSLA --strict
```

**可能原因 → 修复**:

| 发现 | 原因 | 修复 |
|------|------|------|
| `label_coverage < 0.5` | 标签未计算 | `python tools/recompute_labels_1m.py --symbols TSLA --horizons 30` |
| `leakage_detected` | 标签使用未来数据 | 修复 `recompute_labels_1m.py` 逻辑 |
| `drifted_features` | Active/Baseline不一致 | `python tools/update_features_baseline.py` |
| `excessive_timestamp_gaps` | 数据缺失 | `python tools/backfill_bars.py` |

---

## 四、集成到 ML 管道

### 修改 `nightly_oof_flow.py`

在训练前自动运行数据质量检查:

```python
from tools.data_quality_advanced import DataQualityMonitor

def run_oof_with_validation(symbols, window_days=30):
    # 1. 运行数据质量检查
    monitor = DataQualityMonitor()
    all_passed, report = monitor.run_all_checks(
        symbols=symbols,
        lookback_days=window_days,
        horizon_min=30,
        features_table="trading.features_1m_active"
    )

    if not all_passed:
        logger.error("data_quality_failed", report=report)
        return {"ok": False, "error_code": "data_quality_failed", "report": report}

    # 2. 数据质量通过,开始训练
    result = run_oof_cli(...)
    return result
```

---

## 五、告警配置

### Slack 通知示例

```python
import requests

def send_slack_alert(report):
    if not report["summary"]["all_passed"]:
        failed_checks = []
        if not report["summary"]["drift_passed"]:
            failed_checks.append("Feature Drift")
        if not report["summary"]["leakage_passed"]:
            failed_checks.append("Label Leakage")
        if not report["summary"]["alignment_passed"]:
            failed_checks.append("Data Alignment")

        message = {
            "text": f"🚨 Data Quality Failed: {', '.join(failed_checks)}",
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Timestamp*: {report['timestamp']}"}
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Symbols*: {', '.join(report['symbols'])}"}
                }
            ]
        }

        requests.post(os.getenv("SLACK_WEBHOOK_URL"), json=message)
```

---

## 六、常见问题 (FAQ)

### Q1: 漂移检测为何使用 KS 检验而非 t-test?

A: KS 检验对分布形状敏感,可检测均值/方差/偏度的变化,而 t-test 只检测均值差异。金融特征通常非正态分布,KS 更鲁棒。

### Q2: `label_coverage < 0.5` 是否正常?

A: 取决于 `horizon_min`:
- 30分钟 horizon: 每日最后30分钟无法计算标签,覆盖率约 92% 正常
- 如果 < 50%: 表明大量时段标签缺失,需运行 `recompute_labels_1m.py`

### Q3: 如何调整漂移阈值?

A: 修改 `--drift-threshold`:
- 0.05 (默认): 标准显著性水平
- 0.01: 更严格,适合高频策略
- 0.10: 更宽松,适合低频策略

### Q4: 检测到泄露,但逻辑正确?

A: 可能原因:
1. 标签与当前波动率/趋势天然相关 (非泄露)
2. 验证方法: 检查 `recompute_labels_1m.py` 是否使用 `ts + horizon` 之后的数据
3. 运行独立回测验证

---

## 七、参考文献

- Preflight 检查: `scripts/ai_trader/brain/preflight.py`
- 标签计算: `tools/recompute_labels_1m.py`
- 特征管道: `docs/SFS_QUICK_REFERENCE.md`
- ML 管道: `docs/ML_PIPELINE.md`

---

**最后更新**: 2025-12-02
**维护者**: Trading Team
**相关文档**: `CLAUDE.md` 第六章 "SFS架构"
