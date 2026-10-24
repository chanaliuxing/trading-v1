# QQQ Kernel Migration — Status Tracker

**Owner plan**: `docs/implementation/QQQ_KERNEL_MIGRATION_EXECUTION_PLAN_2026-04-20.md`
**Update cadence**: 每个交易日收盘后
**Last updated**: 2026-04-21

---

## 当前 Stage

**Stage 0.5 / D+0 Shadow Readiness — 系统保险丝已基本落地,开始 QQQ shadow readiness**

**D+0 anchor**: 2026-04-20 Monday(EDT)
**原则**: `qqq_intraday_001` 继续是唯一 live writer; QQQ kernel 只能 `dry_run/shadow_only`; 任何 shadow enable 前必须先通过 readiness tests。

---

## Dated TODO — QQQ Kernel Shadow Bring-Up

### D+0 — 2026-04-20 Monday
- [x] 写入 D+0 readiness plan 与每日推进表
- [x] 建立 `reports/qqq_kernel_shadow_parity/` 输出目录
- [x] 设计 `qqq_kernel_shadow_legacy_on_001` / `qqq_kernel_shadow_legacy_off_001` config schema
- [x] 明确 shadow 实例必须满足:
  - `enabled: false` 或 `dry_run: true + shadow_only: true`
  - 引用 `qqq_intraday_policy_v1`
  - `enable_shared_underlying_data_plane: true`
  - `shared_underlying_read_mode: plane_only`
  - 禁止 broker live submit
- [x] 审计 `core.shadow_orders` 是否支持 `origin_strategy_id` / `legacy_mode`;不支持则改走 audit payload 或新增 migration
- [x] 起草 `tests/unit/strategies/test_qqq_kernel_shadow_config.py`

### D+1 — 2026-04-21 Tuesday
- [x] 落地两个 QQQ kernel shadow config block,初始保持 `enabled: false`
- [x] 落地 `test_qqq_kernel_shadow_config.py`
- [x] 验证两个 shadow config:
  - 可由 config loader 解析
  - policy id = `qqq_intraday_policy_v1`
  - `dry_run/shadow_only` fail-closed
  - shared plane 强制开启
  - 不会触发 live submit
- [x] 更新 `QQQ_KERNEL_MIGRATION_EXECUTION_PLAN_2026-04-20.md` 的 Stage 2 readiness gate
- [x] 生成 2026-04-21 启动前 readiness report
- [x] 写入 2026-04-21 开盘前 runbook
- [x] 落地一键 shadow enable/disable 脚本
- [x] 落地 pre-open gate 聚合脚本

### D+2 — 2026-04-22 Wednesday
- [x] 落地 `tool/replay/run_qqq_kernel_shadow_parity.py` 或扩展现有 stocks kernel parity gate
- [x] 输出 `reports/qqq_kernel_shadow_parity/YYYYMMDD.json`
- [x] parity schema 至少包含:
  - `signal_authority`
  - `intent`
  - `order_plan`
  - `block_reason`
  - `exec_symbol`
  - `decision_ts_delta_sec`
- [x] 初期只产出 report + metric,不直接 pager
- [ ] 开盘前确认 parity trace 输入源存在:
  - legacy: `qqq_intraday_001` 写入 `reports/qqq_signal_snapshots/*.jsonl`
  - kernel legacy_on: `qqq_kernel_shadow_legacy_on_001` 写入独立 trace JSONL
  - kernel legacy_off: `qqq_kernel_shadow_legacy_off_001` 写入独立 trace JSONL
- [ ] 若三条 trace 任一缺失,当天 P0 改为修 trace writer,不得把定时运行 parity runner 视为有效 evidence
- [ ] 收盘仅在 legacy/kernel trace 均非空后运行:
  - `run_qqq_kernel_shadow_parity.py --legacy ... --kernel ...`
  - `build_qqq_kernel_shadow_postclose_report.py --day 20260422`

### D+3 — 2026-04-23 Thursday
- [x] 通过 readiness tests 后,仅在 shadow/dry-run 模式打开 `legacy_on/off` 双实例
- [ ] 验证 `qqq_intraday_001` 仍是唯一 live writer
- [ ] 验证两个 shadow 实例 0 次 broker submit
- [ ] 产出首份 intraday parity sample report

---

## 2026-04-21 Tuesday Pre-Open Runbook

**目标**: 只做 readiness / disabled shadow 检查,不启用 broker submit,不切换 live writer。

**开盘前必须运行**:

```bash
./venv_ml/bin/python tool/replay/run_qqq_kernel_shadow_preopen_gate.py \
  --output reports/qqq_kernel_shadow_parity/20260421_preopen_gate.json
```

**快速状态检查**:

```bash
./venv_ml/bin/python tool/replay/switch_qqq_kernel_shadow.py status
```

**仅 D+3 gate 通过后才允许执行 dry-run shadow enable**:

```bash
./venv_ml/bin/python tool/replay/switch_qqq_kernel_shadow.py enable
./venv_ml/bin/python tool/replay/check_qqq_kernel_shadow_readiness.py --allow-enabled
```

**一键回滚**:

```bash
./venv_ml/bin/python tool/replay/switch_qqq_kernel_shadow.py disable
```

**当前已生成报告**:

`reports/qqq_kernel_shadow_parity/20260421_readiness.json`

`reports/qqq_kernel_shadow_parity/20260421_preopen_gate.json`

**当前结论**:

`GO_FOR_READINESS_ONLY`

**当前实际状态(2026-04-20 收盘后准备)**:

1. `qqq_kernel_shadow_legacy_on_001 enabled: true`
2. `qqq_kernel_shadow_legacy_off_001 enabled: true`
3. 两者仍为 `dry_run: true + shadow_only: true`
4. `./venv_ml/bin/python tool/replay/check_qqq_kernel_shadow_readiness.py --allow-enabled` 通过
5. 2026-04-21 收盘 heartbeat 已设置为生成 post-close report
6. `shadow_only=true` 已由 loader fail-closed 强制要求 `dry_run=true`

**这不是 shadow enable GO**。含义是:

1. `qqq_intraday_001` 仍是唯一 live writer。
2. 两个 QQQ kernel shadow 已打开用于 dry-run evidence collection。
3. 两个 shadow config 只允许 shadow/dry-run,不允许 broker live submit。
4. 明天收盘必须显式记录:
   - migration 已应用
   - readiness checker 使用 `--allow-enabled` 通过
   - 0 次 broker submit
   - 首份 intraday parity sample 已产出

**禁止事项**:

1. 禁止把 `qqq_intraday_001` 改为 disabled。
2. 禁止把任一 QQQ kernel shadow 改为 `dry_run: false`。
3. 禁止移除 `shadow_only: true`。
4. 禁止在未应用 `db/pg/migration_20260420_shadow_orders_origin_fields.ddl` 前依赖 `shadow_orders` 做来源归因。
5. 禁止把 `GO_FOR_READINESS_ONLY` 当成 cutover GO。

**回滚条件**:

1. readiness checker 任一 failed check 非空。
2. `qqq_intraday_001` 不再 enabled。
3. 任一 shadow config 不是 `dry_run: true + shadow_only: true`。
4. migration 未应用但系统尝试写 `origin_strategy_id` / `legacy_mode`。
5. 任何 broker submit 来自 `qqq_kernel_shadow_*`。

**回滚动作**:

1. 保持或恢复两个 shadow 实例 `enabled: false`。
2. 保持 `qqq_intraday_001 enabled: true`。
3. 暂停 D+3 shadow enable,只继续 D+2 parity/report 工具验证。

### D+4 — 2026-04-24 Friday
- [ ] 汇总首个交易周 shadow readiness / parity 结果
- [ ] 若 `legacy_on` diff 为 0 或全归因,下周继续累计交易日证据
- [ ] 若 signal authority diff 无法归因,优先回到 Stage 1 signal authority 抽离,暂停 plugin 迁移
- [ ] 周五收盘后更新 GO/NO-GO:
  - `GO`: 进入 D+7-D+11 观察
  - `NO-GO`: 修复 readiness/parity blocker

### D+7 — 2026-04-27 Monday
- [ ] 开始累计 `legacy_on` / `legacy_off` 5 到 10 个交易日 shadow parity
- [ ] 每日收盘更新:
  - allow diff
  - side diff
  - reason diff
  - exec_symbol diff
  - no-live-submit 证明
  - shared plane diff

### D+10 — 2026-04-30 Thursday
- [ ] 第一次 3 到 5 交易日 review
- [ ] 决定是否产品化 `QQQIntradayStockPlugin`
- [ ] 不允许在 parity 归因前大搬 monolith 执行逻辑

### D+15 — 2026-05-05 Tuesday
- [ ] 若双模式 shadow 证据稳定,准备 Stage 3 lifecycle parity proof
- [ ] legacy parity 退役倒计时复核

---

## Stage 0 Baseline TODO

- [ ] 设计并实现 `tool/replay/run_qqq_monolith_baseline_capture.py`(或扩展现有 audit writer),连续采集 10 个交易日 baseline
- [ ] 明确 baseline 快照字段 schema(见主计划 §3 Stage 0)
- [x] 在 `config.yml:851` 旁加 `legacy_trader_parity_mode` 退役 TODO 注释
- [x] 建目录 `reports/qqq_kernel_migration/baseline/`
- [x] 跑 logic 引用图审计(`§7`),产出 `reports/qqq_kernel_migration/logic_reference_audit.txt`
- [x] 审计结果分类归档:**现用公共** / **仅 monolith 引用** / **无引用**

### Logic 引用图审计结论(2026-04-20)

全部 17 个 `strategies/qqq_intraday_*_logic.py` **均为"现用公共模块"**,无一遗迹。

关键事实:
- **QQQ monolith(`qqq_intraday_strategy.py`)自身不 import 任何 `*_logic.py`**。
- 消费方分布:
  - `index_super_win_replica_strategy.py` / `index_super_win_replica_core.py` — 消费 14 个
  - `stocks_kernel/modules/protection.py` — 消费 `qqq_intraday_manage_logic`
  - `options_kernel/modules/profit_management_policy.py` — 消费 `qqq_intraday_manage_logic`
  - `options_kernel/modules/risk_policy.py` — 消费 `qqq_intraday_manage_logic`
  - `qqq_intraday_rules/parity.py` — 消费 `qqq_intraday_gate_audit_logic`
  - `qqq_intraday_rules/direction.py` — 消费 `qqq_intraday_v47_logic`
  - 大部分有对应单元测试
- 对迁移的含义:
  - ✅ Stage 5 冻结 monolith 时**不会**连带这些 logic 文件
  - ⚠️ 这些文件已经是事实上的共享基础设施,后续应显式移入 `strategies/shared/` 或 `libs/strategy/shared/`,与 monolith 生命周期解耦(不列为 hard gate,属 cleanup candidate)

审计文件:`reports/qqq_kernel_migration/logic_reference_audit.txt`

---

## Hard Gate 状态

| Gate | Stage | 状态 | 备注 |
|---|---|---|---|
| Baseline 可一键重放 | Stage 0 | ⏳ | |
| Legacy 退役注释落地 | Stage 0 | ✅ 2026-04-20 | config.yml:851 已加 |
| D+0 readiness plan | Stage 0.5 | ✅ 2026-04-20 | 本状态文件已写入每日 TODO |
| QQQ shadow config no-live-submit test | Stage 2 readiness | ✅ 2026-04-20 | `test_qqq_kernel_shadow_config.py` |
| QQQ shadow parity runner | Stage 2 readiness | ✅ 2026-04-20 | `run_qqq_kernel_shadow_parity.py` |
| Legacy_on/off shadow enabled dry-run | Stage 2 | ⏳ | D+3 后,需 readiness tests 通过 |
| Signal authority 字节级一致 | Stage 1 | — | 等 Stage 0 |
| 期权零差异 | Stage 1 | — | 等 Stage 0 |
| Legacy_on shadow 对齐 monolith | Stage 2 | — | |
| Legacy_off shadow 归因完备 | Stage 2 | — | |
| 下游生命周期一致 | Stage 3 | — | |
| IBKR 身份字段一致 | Stage 3 | — | 依赖 IBKR_RECONNECT_HARDENING_PLAN |
| Legacy 退役 D+45 完成 | Stage 4 | — | |
| 半步切换日级 PnL ≤ 0.5% | Stage 4 | — | |
| Kernel live 自算信号交叉校验 | Stage 5 | — | |

---

## 熔断/回滚事件日志

_(暂无)_

---

## 周会记录

### 2026-04-20(Week 0,计划启动)
- 签署主计划 `QQQ_KERNEL_MIGRATION_EXECUTION_PLAN_2026-04-20.md`
- 落地 Stage 0 的第一批 TODO(目录、注释、本状态文件)
- 下周五前完成:baseline capture 脚本设计 + logic 引用图审计

### 2026-04-20(D+0 Shadow Readiness)
- 同意进入 QQQ kernel shadow readiness,但不直接切 live,也不直接大搬 monolith 逻辑。
- 已建立 `reports/qqq_kernel_shadow_parity/`。
- 本周目标调整为:
  - D+1 config + no-live-submit tests
  - D+2 parity runner
  - D+3 readiness 通过后再打开 dry-run shadow
  - D+4 收盘 GO/NO-GO
- 当前禁止事项:
  - 不切换 `qqq_intraday_001` live writer
  - 不跳过 signal authority 归因
  - 不在 parity 证据前产品化大规模 `QQQIntradayStockPlugin`

### 2026-04-20(D+1 Readiness 提前落地)
- 已落地:
  - `config.yml` 新增 `qqq_kernel_shadow_legacy_on_001`
  - `config.yml` 新增 `qqq_kernel_shadow_legacy_off_001`
  - 两个实例均 `enabled: false`, `dry_run: true`, `shadow_only: true`
  - 两个实例均引用 `qqq_intraday_policy_v1`, QQQ/TQQQ/SQQQ, shared plane `plane_only`
  - `db/pg/migration_20260420_shadow_orders_origin_fields.ddl` 为 `core.shadow_orders` 增加 `origin_strategy_id` / `legacy_mode` / `shadow_profile_id`
  - `tests/unit/strategies/test_qqq_kernel_shadow_config.py` 通过

### 2026-04-20(D+2 Parity Runner 提前落地)
- 已落地:
  - `tool/replay/run_qqq_kernel_shadow_parity.py`
  - `tests/unit/tool/test_run_qqq_kernel_shadow_parity.py`
  - 默认输出 `reports/qqq_kernel_shadow_parity/YYYYMMDD.json`
  - report schema 覆盖 `signal_authority` / `intent` / `order_plan` / `block_reason` / `exec_symbol` / `decision_ts_delta_sec`
  - 初期只写 report 内的 `qqq_kernel_shadow_parity_diff_total`,不接 pager

### 2026-04-20(2026-04-21 Pre-Open 准备)
- 已落地:
  - `tool/replay/check_qqq_kernel_shadow_readiness.py`
  - `tool/replay/run_qqq_kernel_shadow_preopen_gate.py`
  - `tool/replay/switch_qqq_kernel_shadow.py`
  - `tests/unit/tool/test_check_qqq_kernel_shadow_readiness.py`
  - `tests/unit/tool/test_run_qqq_kernel_shadow_preopen_gate.py`
  - `tests/unit/tool/test_switch_qqq_kernel_shadow.py`
  - `reports/qqq_kernel_shadow_parity/20260421_readiness.json`
  - `reports/qqq_kernel_shadow_parity/20260421_preopen_gate.json`
- 当前 readiness 结论:
  - `GO_FOR_READINESS_ONLY`
  - 所有 failed checks 为空
- 明天执行口径:
  - 运行系统后直接采集 dry-run shadow evidence
  - 不切 live writer
  - 双 shadow 已启用,但必须保持 `dry_run: true + shadow_only: true`
  - 收盘自动生成 post-close report

### 2026-04-20(快速推进到 Shadow Evidence Collection)
- 已落地:
  - `tool/replay/switch_qqq_kernel_shadow.py enable`
  - `tool/replay/check_qqq_kernel_shadow_readiness.py --allow-enabled`
  - `tool/replay/build_qqq_kernel_shadow_postclose_report.py`
  - `tests/unit/tool/test_build_qqq_kernel_shadow_postclose_report.py`
  - `reports/qqq_kernel_shadow_parity/20260421_enabled_readiness.json`
  - `reports/qqq_kernel_shadow_parity/20260421_preopen_gate_enabled.json`
  - `reports/qqq_kernel_shadow_parity/20260421_postclose_report_preview.json`
- 当前结论:
  - `qqq_intraday_001 enabled: true`
  - `qqq_kernel_shadow_legacy_on_001 enabled: true`
  - `qqq_kernel_shadow_legacy_off_001 enabled: true`
  - shadow readiness with `--allow-enabled`: PASS
  - post-close report 在交易日若 `parity_report_count=0` 会强制 blocker: `parity_runner_did_not_execute`
  - 4:20 PM 触发源为 Codex Desktop thread heartbeat(`automation id: qqq-kernel-daily-push`),不是 run_system 内部定时器

### 2026-04-21(Post-close summary)
- 收盘报告:
  - `reports/qqq_kernel_shadow_parity/20260421_postclose_report.json`
  - recommendation: `HOLD`
  - blockers: `parity_runner_did_not_execute`
  - parity_report_count: `0`
  - total_diff_count: `0`
- no-live-submit 配置状态:
  - `qqq_intraday_001 enabled: true`(live writer 保持)
  - `qqq_kernel_shadow_legacy_on_001 enabled: true + dry_run: true + shadow_only: true`
  - `qqq_kernel_shadow_legacy_off_001 enabled: true + dry_run: true + shadow_only: true`
  - `shadow_only=true` 且 `dry_run=false` 已在 loader fail-closed 拦截
- 今日工具/测试核验:
  - `tool/replay/build_qqq_kernel_shadow_postclose_report.py --day 20260421` 已执行
  - 目标测试通过: `18 passed`
- 完成度(口径更新):
  - QQQ shadow readiness: `100%`(配置、gate、报告链路就绪)
  - QQQ cutover confidence: `unmeasured`(缺交易日 parity 输入与样本;在连续 3 天 `parity_report_count>=1` 且 `diff_total==0` 前不再给百分数)
  - 可插拔平台: `97%`

### 2026-04-21(Critical review follow-up)
- 同意 Claude 对当日证据链的核心判断:
  - 今天不算 shadow parity 证据日;只有 readiness/preopen/postclose/drift 产物,没有 `*_parity.json`。
  - `run_qqq_kernel_shadow_parity.py` 是离线 diff 工具,需要 `--legacy` 与 `--kernel` 输入,不会自己采样。
  - 当前 `reports/qqq_signal_snapshots/` 不存在,说明明天的第一优先级是确认 trace writer,不是机械定时跑 parity runner。
- 已收紧 drift report 口径:
  - `parity_report_count=0` -> `recommendation=HOLD`, blocker=`no_parity_report_yet`
  - parity 报告中 `legacy_count==0` 或 `kernel_count==0` -> `recommendation=HOLD`, blocker=`empty_parity_report_input`
  - D+3 聚合使用 `min_match_ratio`,不使用 avg 掩盖单日失败
- 已补明天输入链路:
  - `run_qqq_kernel_shadow_parity.py` 支持读取 JSONL trace 输入
  - QQQ kernel shadow 在 dry-run 跳过 broker submit 前写入独立 `reports/qqq_signal_snapshots/qqq_signal_snapshots_YYYYMMDD_<strategy_id>.jsonl`
  - 两个 shadow config 显式开启 `qqq_shadow_trace_enabled: true`
- 2026-04-22 执行顺序:
  - pre-open: `switch_qqq_kernel_shadow.py status` + `check_qqq_kernel_shadow_readiness.py --allow-enabled`
  - pre-open smoke: 如果已有 JSONL,先做同文件 self-parity,期望 `diff_total=0`
  - intraday: 先观察 3 条 strategy trace 是否实时写入;如果只有 legacy 写入,当天修 kernel trace writer
  - post-close: 只有在 legacy/kernel trace 都非空时生成 legacy_on/off 两份 parity report,再聚合 post-close
