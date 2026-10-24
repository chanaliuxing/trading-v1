# AI 大脑自动化标签修复指南

**目标**: 让 AI 大脑可以自动检测和修复标签质量问题

**日期**: 2025-11-26
**状态**: ✅ 生产代码已修复，AI 大脑可自动调用

---

## 一、问题回顾

### 之前的手动修复

**问题**:
- 标签生成代码有 bug (leadInFrame=0 → label=-1.0, close=0 → inf)
- 修复方案在 `/tmp/` 临时目录
- AI 大脑无法自动调用修复工具

**手动修复**:
```bash
# 临时解决方案
venv/bin/python /tmp/generate_labels_chunked_v2.py
```

**缺点**:
- ❌ 需要人工运行
- ❌ 不是生产代码
- ❌ AI 大脑无法自己修复

---

## 二、自动化解决方案 (已实现)

### ✅ 修复了生产代码

**修复的文件**:
1. `tools/recompute_labels_1m.py` - Pandas 版本 (DataHealer 使用)
2. `pipelines/generate_labels.py` - SQL 版本 (手动/cron 使用)

**关键修复**:

#### 1. tools/recompute_labels_1m.py (Pandas)

```python
# ✅ FIX: Safe division with edge case handling
for h in horizons:
    fwd = df["close"].shift(-h)

    # Prevent division by zero and handle NaN from shift
    import numpy as np
    with np.errstate(divide='ignore', invalid='ignore'):
        lbl = (fwd / df["close"] - 1.0)

    # Replace inf/-inf with NaN (caused by close=0)
    lbl = lbl.replace([np.inf, -np.inf], np.nan)

    # Cast to Float64 (preserves NaN for tail and edge cases)
    df[f"label_h{h}"] = lbl.astype("Float64")
```

**效果**:
- ✅ 最后 30 行正确为 NaN (不是 -1.0)
- ✅ close=0 时返回 NaN (不是 inf)
- ✅ 验证通过: 360 valid / 390 total (30 NaN at tail)

#### 2. pipelines/generate_labels.py (SQL)

```python
# ✅ FIX: Use leadInFrame + CASE statement
CASE
    WHEN f.close = 0 THEN NULL                   -- Prevent division by zero
    WHEN f.close IS NULL THEN NULL
    WHEN f.lead_close_30 = 0 THEN NULL           -- leadInFrame returns 0 at tail
    WHEN f.lead_close_30 IS NULL THEN NULL
    ELSE (f.lead_close_30 - f.close) / f.close   -- Safe division
END AS label_h30
```

---

## 三、AI 大脑自动调用流程

### 现有架构 (已支持)

```
用户观察到标签问题 (e.g. HPO gates 失败)
         ↓
AI 大脑分析诊断 (via Orchestrator)
         ↓
AI 大脑写入 pending_actions.yml:
    - action: label_recompute
      params:
        symbols: ["AAPL", "TSLA", ...]
        horizons: [30]
        lookback_days: 90
         ↓
Orchestrator 执行 (scripts/ai_trader/orchestrator.py)
         ↓
调用 DataHealer.ensure_labels_ready()
         ↓
调用 tools/recompute_labels_1m.py (已修复✅)
         ↓
生成干净标签 (无 -1.0, 无 inf)
         ↓
结果写入 label_recompute_log.json
         ↓
AI 大脑读取结果，确认修复成功
```

### 代码路径

**1. Orchestrator 支持** (`scripts/ai_trader/orchestrator.py:4782-4861`):
```python
elif act == "label_recompute":
    symbols = params.get("symbols", [])
    horizons = params.get("horizons", [30])
    lookback_days = params.get("lookback_days", 90)

    if symbols and self.data_healer:
        label_result = self.data_healer.ensure_labels_ready(
            symbols=symbols,
            horizons=horizons,
            lookback_days=lookback_days,
            batch_size=5,
            max_retries=2
        )

        label_recompute_results.append({
            "symbols": symbols,
            "horizons": horizons,
            "lookback_days": lookback_days,
            "result": label_result
        })
```

**2. DataHealer 实现** (`services/data/data_healer.py:535-699`):
```python
def ensure_labels_ready(
    self,
    symbols: List[str],
    horizons: List[int] = [30],
    lookback_days: int = 90,
    batch_size: int = 5
) -> dict:
    """
    确保标签数据就绪 (批量生成避免内存溢出)

    Returns:
        {
            "status": "fixed" | "failed" | "already_ok",
            "missing_symbols": [...],
            "generated_rows": 123456,
            "errors": [...]
        }
    """
    # 1. 检测哪些 symbol 缺少标签
    missing_symbols = self._check_label_coverage(symbols, horizons, lookback_days)

    # 2. 分批生成标签 (每批 5 个 symbol)
    for batch in batches:
        rows_generated = self._generate_labels_batch(
            batch,
            horizons,
            lookback_days
        )
        # 调用 tools/recompute_labels_1m.py (已修复✅)

    # 3. 验证修复结果
    remaining_missing = self._check_label_coverage(...)

    return {"status": status, "generated_rows": total_rows_generated, ...}
```

**3. 底层工具** (`tools/recompute_labels_1m.py`):
```python
# AI 大脑不直接调用，通过 DataHealer 调用
# 已修复 ✅:
# - 正确处理 shift(-30) 返回的 NaN
# - 正确处理 close=0 的情况
```

---

## 四、AI 大脑使用示例

### 场景 1: HPO Gates 失败 (标签问题)

**AI 大脑观察到**:
```python
# 从 Orchestrator 日志
{
    "event": "alpha_loop_gates_failed",
    "gates": {
        "min_ic": {"value": 0.02, "threshold": 0.05, "passed": false}
    },
    "reason": "标签质量可能有问题"
}
```

**AI 大脑诊断**:
```python
# Brain 分析
- 检查 features_1m_with_labels 表
- 发现: countIf(label_h30 = -1.0) > 0 或 countIf(label_h30 = inf) > 0
- 结论: 标签生成有 bug
```

**AI 大脑决策 & 执行**:
```yaml
# Brain 写入 runs/ai_trader/{date}/pending_actions.yml
pending:
  - action: label_recompute
    params:
      symbols: ["AAPL", "AMD", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA"]
      horizons: [30]
      lookback_days: 90
      reason: "Detected -1.0 contamination in labels, need regeneration"
```

**Orchestrator 自动执行**:
```bash
# 下次 Orchestrator 运行时
1. 读取 pending_actions.yml
2. 调用 DataHealer.ensure_labels_ready()
3. 生成干净标签 (使用修复后的代码 ✅)
4. 写入 label_recompute_log.json
```

**AI 大脑验证**:
```python
# Brain 下次运行时
log_data = read("label_recompute_log.json")
if log_data["results"][0]["result"]["status"] == "fixed":
    print("✅ 标签修复成功")
    # 可以继续运行 HPO
```

---

## 五、监控和质量保证

### 自动化质量检查

**1. 每日健康检查** (可由 AI 大脑触发):

```sql
-- 检查标签质量
SELECT
    countIf(label_h30 = -1.0) as bad_neg_one,
    countIf(label_h30 = inf) as bad_inf,
    countIf(label_h30 IS NULL) as null_count,
    count(*) as total
FROM trading.features_1m_with_labels
WHERE ts > now() - INTERVAL 7 DAY
```

**预期**:
- `bad_neg_one` = 0
- `bad_inf` = 0
- `null_count` / `total` ≈ 2-3% (仅序列尾部)

**2. 标签时效性检查**:

```sql
SELECT
    max(ts) as latest_label,
    now() - max(ts) as staleness_hours
FROM trading.features_1m_with_labels
```

**告警**:
- 如果 `staleness_hours` > 48 → AI 大脑触发 `label_recompute`

---

## 六、生产部署清单

### ✅ 已完成

- [x] 修复 `tools/recompute_labels_1m.py` (Pandas 版本)
- [x] 修复 `pipelines/generate_labels.py` (SQL 版本)
- [x] 验证修复效果 (0 -1.0, 0 inf)
- [x] DataHealer.ensure_labels_ready() 集成
- [x] Orchestrator label_recompute action 支持

### 📋 可选增强

- [ ] 添加 Prometheus metrics: `label_quality_score` gauge
- [ ] 添加 cron job: 每日凌晨重新生成标签
- [ ] AI 大脑 proactive monitoring: 定期检查标签质量
- [ ] 添加 Slack 告警: 当检测到 -1.0/inf 时通知

---

## 七、对比：手动 vs 自动化

| 方面 | 之前 (手动) | 现在 (自动化) |
|------|------------|-------------|
| **检测问题** | 人工查看日志 | AI 大脑自动检测 |
| **诊断** | 人工运行诊断脚本 | AI 大脑分析 ClickHouse |
| **修复** | 运行 `/tmp/` 临时脚本 | AI 大脑写 pending_actions.yml |
| **执行** | `venv/bin/python /tmp/...` | Orchestrator 自动执行 |
| **验证** | 人工查询 ClickHouse | AI 大脑读 log.json |
| **工具** | 临时脚本 | 生产代码 ✅ |
| **可靠性** | 人工可能出错 | 自动化流程 |
| **时效** | 发现后几小时 | 发现后几分钟 |

---

## 八、使用场景示例

### 场景 A: 新策略需要历史标签

**AI 大脑决策**:
```yaml
pending:
  - action: label_recompute
    params:
      symbols: ["新增的股票"]
      horizons: [30]
      lookback_days: 365  # 需要 1 年历史
```

### 场景 B: 发现标签数据陈旧

**AI 大脑检测**:
```python
# Brain 查询
latest_label_ts = clickhouse.query("SELECT max(ts) FROM features_1m_with_labels")
if now() - latest_label_ts > 48_hours:
    # 触发重新生成
```

### 场景 C: 数据库被错误清空

**AI 大脑恢复**:
```yaml
pending:
  - action: label_recompute
    params:
      symbols: ["所有交易的股票"]
      horizons: [30]
      lookback_days: 365
```

---

## 九、常见问题

### Q1: AI 大脑如何知道标签需要修复？

**A**: 多种检测方式:
1. **Gates 失败**: `min_ic < threshold` → 可能标签问题
2. **主动检查**: 每日查询 `countIf(label_h30 = -1.0)`
3. **时效性检查**: `max(ts)` 过于陈旧
4. **HPO 异常**: 训练结果异常 (Sharpe 突然下降)

### Q2: 修复需要多长时间？

**A**: 取决于数据量:
- **5 symbols × 90 days**: ~2-3 分钟
- **15 symbols × 365 days**: ~10-15 分钟
- **批处理**: 每批 5 symbols,自动分批避免超时

### Q3: 如果 AI 大脑判断错误怎么办？

**A**: 有安全机制:
1. **Dry-run 模式**: 可先验证不实际执行
2. **日志记录**: 所有操作记录在 `label_recompute_log.json`
3. **可回滚**: ClickHouse ReplacingMergeTree 支持重新生成
4. **人工审查**: 可在 pending_actions.yml 中添加 `require_approval: true`

### Q4: 新增股票如何自动生成标签？

**A**: 两种方式:
1. **被动**: AI 大脑在 HPO 前检查，发现缺失自动生成
2. **主动**: 配置 cron job 每日检查并生成

---

## 十、总结

### ✅ 实现的自动化

1. **生产代码已修复**:
   - `tools/recompute_labels_1m.py` ✅
   - `pipelines/generate_labels.py` ✅

2. **AI 大脑可自动调用**:
   - 通过 `label_recompute` action
   - DataHealer 自动批处理
   - 结果自动记录

3. **质量保证**:
   - 无 -1.0 contamination
   - 无 inf values
   - 正确处理序列尾部 NaN

### 🎯 核心优势

**从手动修复 → 自动化修复**:
- ⏱️ 从发现到修复: **几小时 → 几分钟**
- 🤖 无需人工介入
- 📊 自动质量验证
- 🔄 可重复、可靠

**AI 大脑的能力**:
- ✅ 自动检测标签问题
- ✅ 自动触发修复
- ✅ 自动验证结果
- ✅ 自动记录日志

---

**最后更新**: 2025-11-26 14:11 PST
**维护者**: Trading Team
**状态**: ✅ PRODUCTION READY

**相关文档**:
- `docs/LABEL_GENERATION_FINAL_SOLUTION.md` - 完整修复方案
- `services/data/data_healer.py:535-699` - 源码
- `scripts/ai_trader/orchestrator.py:4782-4861` - Action handler
