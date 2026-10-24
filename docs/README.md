
---

## 每日运维工具 (2025-10-27 新增)

完整的每日运维工具包已整理到 `daily/` 目录：

- **[daily/README.md](daily/README.md)** - 工具包总览
- **[daily/QUICK_REFERENCE.md](daily/QUICK_REFERENCE.md)** - 快速参考卡（建议打印）
- **[daily/DAILY_OPERATIONS_CHECKLIST.md](daily/DAILY_OPERATIONS_CHECKLIST.md)** - 完整清单
- **[daily/morning_check.sh](daily/morning_check.sh)** - 盘前检查脚本
- **[daily/evening_review.sh](daily/evening_review.sh)** - 盘后复盘脚本
- **[daily/trigger_backfill.py](daily/trigger_backfill.py)** - 数据回补工具

**快速开始**:
```bash
cd ./docs/daily
./morning_check.sh      # 每日盘前
./evening_review.sh     # 每日盘后
```

## 发布前最小签收与查询导出

按固定顺序执行（不自动放行，不自动改 baseline）：

1) 生成 fixture/research 产物

```bash
venv/bin/python -m scripts.ai_trader.research.run_fixture_cycle
```

产物目录：`runs/ai_trader/research/cycles/fixture-2026-04-05/`

2) 执行 freeze gate

```bash
venv/bin/python -m scripts.ai_trader.research.freeze_change_gate
```

产物：`runs/ai_trader/research/state/research_safety_contract_gate_report.json`

3) 生成 signoff summary

```bash
venv/bin/python -m scripts.ai_trader.research.release_signoff_summary
```

产物：`runs/ai_trader/research/state/release_signoff_summary.json`

4) 记录 candidate batch

```bash
venv/bin/python -m scripts.ai_trader.research.candidate_batch_record --batch-id <batch_id> --run-id <run_id> --cycle-id fixture-2026-04-05
```

产物：`runs/ai_trader/research/state/release_candidate_batch_records.json`

5) 生成 audit summary

```bash
venv/bin/python -m scripts.ai_trader.research.candidate_batch_audit_summary --keep-last-records 10
```

产物：`runs/ai_trader/research/state/release_candidate_batch_audit_summary.json`

6) 查询并导出 candidate batch

```bash
venv/bin/python -m scripts.ai_trader.research.candidate_batch_query --candidate-batch-status candidate_batch_blocked --latest-n 5 --export-path runs/ai_trader/research/state/release_candidate_batch_query_export.json
```

产物：`runs/ai_trader/research/state/release_candidate_batch_query_export.json`

封板与最终摘要：

```bash
venv/bin/python -m scripts.ai_trader.research.release_seal_summary --release-tag <release_tag>
```

产物：`runs/ai_trader/research/state/release_seal.json`、`runs/ai_trader/research/state/release_final_delivery_summary.json`

最终移交：

```bash
venv/bin/python -m scripts.ai_trader.research.release_handoff --handoff-id <handoff_id>
```

产物：`runs/ai_trader/research/state/release_handoff_manifest.json`、`runs/ai_trader/research/state/release_prechange_checklist_template.json`（改动前先看 checklist template）

阶段 26–36 单页交付索引：

```bash
venv/bin/python -m scripts.ai_trader.research.release_delivery_index --index-id <index_id>
```

产物：`runs/ai_trader/research/state/release_delivery_index.json`（并关联 `runs/ai_trader/research/state/release_seal.json`、`runs/ai_trader/research/state/release_handoff_manifest.json`、`runs/ai_trader/research/state/release_prechange_checklist_template.json`）

维护模式固定顺序（空变更复现）：

```bash
test -f runs/ai_trader/research/state/release_prechange_checklist_template.json
venv/bin/python -m scripts.ai_trader.research.freeze_change_gate
venv/bin/python -m scripts.ai_trader.research.release_signoff_summary
venv/bin/python -m scripts.ai_trader.research.release_delivery_index --index-id <index_id>
venv/bin/python -m scripts.ai_trader.research.release_maintenance_verification --verification-id <verification_id>
```

## Policy Compatibility Observability 只读契约

统一只读视图字段（必选）：

- `status`
- `reason`
- `source_path`
- `counts`
- `current_cycle`
- `recent_examples`
- `migration_progress`

`migration_progress`（只读进度看板）字段：

- `runtime_legacy_consumer_count`
- `total_runtime_consumers`
- `migrated_runtime_consumers`
- `docs_tests_only_references`
- `progress_ratio`

状态枚举与降级语义：

- `ok`：成功读取并解析 `runs/ai_trader/research/state/policy_compatibility_summary.json`
- `degraded`：文件存在但损坏/不可解析
- `unavailable`：文件不存在

兼容别名保留规则：

- `recent_hits` 是 `recent_examples` 的兼容别名，内容应保持一致。
- `policy_compatibility` 为旧消费方兼容映射（从 `policy_compatibility_observability.current_cycle` 派生），新消费方优先读取 `policy_compatibility_observability`。
- 旧字段仍兼容，但**不推荐新增依赖**；当检测到运行时旧字段消费时，展示层会在 `migration_notice` 给出只读迁移提醒。

`migration_notice` 分级与节流（只读）：

- `level`: `none` / `info` / `warn` / `high`
- 同一轮同一库存结果下，三处展示入口输出相同 `summary/level/count` 与 `throttle_key`，避免重复展开刷屏。

消费方清点（机器可读）落盘：

- `runs/ai_trader/research/state/policy_observability_consumers.json`

示例（基于真实产物字段，节选）：

```json
{
  "status": "ok",
  "reason": "ready",
  "source_path": "runs/ai_trader/research/state/policy_compatibility_summary.json",
  "counts": {
    "known": 1,
    "legacy": 0,
    "unknown": 16,
    "degraded": 16,
    "digest_changed_without_version_bump": 0
  },
  "current_cycle": {
    "cycle_id": "fixture-2026-04-05",
    "policy_state": "known",
    "current_policy": {
      "policy_id": "preflight-policy",
      "version": "v3",
      "digest": "digest-v3"
    },
    "history_summary": {
      "history_count": 3
    },
    "anomaly_count": 0,
    "legacy_hint_count": 0,
    "degraded": false
  },
  "migration_progress": {
    "status": "ok",
    "reason": "ready",
    "source_path": "runs/ai_trader/research/state/policy_observability_consumers.json",
    "runtime_legacy_consumer_count": 6,
    "total_runtime_consumers": 8,
    "migrated_runtime_consumers": 2,
    "docs_tests_only_references": 4,
    "progress_ratio": 0.25
  },
  "recent_examples": {
    "legacy": [],
    "unknown": [],
    "degraded": [],
    "digest_changed_without_version_bump": []
  },
  "recent_hits": {
    "legacy": [],
    "unknown": [],
    "degraded": [],
    "digest_changed_without_version_bump": []
  }
}
```
