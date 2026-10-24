# Stocks Kernel Trader Parity / Release Gate Remediation TODO / Test Plan / Acceptance (2026-04-17)

目的：针对最近 review 暴露的 3 个缺口，形成可执行、可回滚、不会误伤现网的整改方案。

范围：

1. `trader parity gate` 必须进入正式 release gate，而不是停留在独立脚本。
2. `trader parity gate` 的检查维度必须覆盖 QQQ 交易语义复刻的关键证据。
3. `same-day flatten` 必须是显式契约，不能再依赖隐式 fallback 推断。

非目标：

1. 不在本次整改中改写 QQQ 规则本身。
2. 不在本次整改中放大到其它策略发布机制的全量重构。
3. 不用“默认开启”的方式把新约束强推到所有现有策略。

## 1. 问题定义

### Finding A: Trader parity gate 没进入正式发布链

当前风险：

1. 文档要求“复刻 QQQ 交易语义后才能准入”，但系统正式发布判断仍可能绕过 parity gate。
2. 这会导致“纸面满足，系统未强制”的假验收。

第一性原理判断：

1. 如果一个验收条件不在最终 `GO/BLOCK` 决策链里，它就不是正式准入条件。
2. 对交易系统来说，发布链必须反映真实准入边界，而不是依赖人工记忆。

### Finding B: Parity gate 维度不完整

当前风险：

1. 只看 `allow/side/fill_count/same_day_flatten` 不够。
2. 策略可能在“为什么做”和“实际执行到哪个标的”两个关键维度上已经偏离，但仍然被判成 `GO`。

第一性原理判断：

1. 交易员视角的“复刻”至少要覆盖：
   - 是否允许做
   - 做哪一边
   - 为什么做
   - 实际打到哪个执行标的
   - 日内节奏是否失真
   - 当日是否闭环

### Finding C: Same-day flatten 仍有隐式 fallback

当前风险：

1. 某条 shadow/test parity 策略如果只是为了做信号对拍，可能因为隐式 fallback 被自动加上 EOD flatten。
2. 这会造成跨策略行为漂移，违反“显式配置、显式行为”的原则。

第一性原理判断：

1. 涉及真实持仓处置的行为不能由 `policy_id + parity flag` 隐式推导。
2. 当日清仓必须是显式契约：要么配置 cutoff，要么配置显式布尔开关。

## 2. 整改目标

本次整改完成后，必须同时满足下面 6 条：

1. 启用 `trader_parity_gate` 的策略，发布链里没有任何路径可以绕过 parity 检查。
2. parity gate 至少覆盖 `allow/side/reason/exec_symbol/fill cadence/EOD closeout` 六类证据。
3. parity gate 失败时，正式 release pipeline 必须输出 `BLOCK`，并给出失败项。
4. `same-day flatten` 只接受显式契约，不再按 policy 或 parity 状态隐式推断。
5. 未启用 parity gate 的策略，发布行为保持不变。
6. 现网其它策略在默认配置下不受本次整改影响。

## 3. 修复方案

### 3.1 Release Gate 整改

方案：

1. `run_stocks_kernel_release_pipeline.py` 中把 `trader parity gate` 变成正式 gate input。
2. 当 `extras.trader_parity_gate.enabled=true` 时：
   - 必须运行 parity gate
   - parity gate 失败必须把最终推荐改为 `BLOCK`
   - 输出 `failed_checks`
3. 当 `extras.trader_parity_gate.enabled=false` 时：
   - 完全走旧路径
   - 不改变非目标策略发布行为

实现要求：

1. 不能只写报告，必须影响最终 `recommendation`
2. 输出文件必须可审计，包含：
   - `trader_parity_gate_enabled`
   - `trader_parity_gate_report_path`
   - `failed_checks`

### 3.2 Parity Gate 维度整改

方案：

1. 保留原有检查：
   - `signal_allow_parity`
   - `signal_side_parity`
   - `cadence_fill_count_guard`
   - `same_day_flatten_guard`
2. 补入强制检查：
   - `signal_reason_parity`
   - `exec_symbol_parity`
3. 输出结构统一：
   - `checks[]`
   - `failed_checks[]`
   - `recommendation`
4. 任一关键维度未达标即 `HOLD`

实现要求：

1. `reason parity` 不得只作为附加信息，必须进入决策。
2. `exec symbol parity` 不得只作为审计字段，必须进入决策。
3. `identity report` 缺失或损坏时，不得 fail-open，必须 `HOLD`。

### 3.3 Same-Day Flatten 契约整改

方案：

1. `SessionModule.eod_flatten_due()` 只认可两种显式来源：
   - `flatten_before_close_et`
   - `enforce_same_day_flatten=true` 且可选 `default_flatten_before_close_et`
2. 移除任何基于：
   - `policy_id`
   - `rule_engine_parity`
   - legacy strategy type
   的隐式 fallback
3. 对 parity 策略的日内清仓要求，下沉到策略配置准入清单，而不是 session 模块猜测。

实现要求：

1. session 模块只执行显式契约，不承担“替策略猜意图”的职责。
2. 发布前检查要明确报出：是否缺少 `flatten_before_close_et` / `enforce_same_day_flatten`。

## 4. TODO List

### P0

1. 把 parity gate 接入正式 release pipeline 的 `GO/BLOCK` 决策链。
2. 在 release pipeline 报告中持久化 parity gate 结果和失败项。
3. 在 parity gate 中强制启用 `reason parity` 检查。
4. 在 parity gate 中强制启用 `exec symbol parity` 检查。
5. 把 identity report 缺失/损坏改成 fail-closed。
6. 删除 `same-day flatten` 的隐式 fallback 路径。
7. 在 trader parity 执行计划文档中更新“显式契约”要求。

### P1

1. 为 release pipeline 增加“parity gate enabled but missing report path”校验。
2. 为目标策略配置增加准入摘要：
   - `rule_engine_parity`
   - `flatten_before_close_et`
   - `no_new_after_et`
   - `trader_parity_gate.enabled`
3. 为 daily summary / identity report 不一致场景增加更可读的失败原因。

## 5. 测试计划

### 5.1 Unit Tests

#### Release Pipeline

1. `trader_parity_gate.enabled=false` 时，pipeline 推荐结果与旧逻辑一致。
2. `trader_parity_gate.enabled=true` 且 parity gate=`GO` 时，pipeline 可正常 `GO`。
3. `trader_parity_gate.enabled=true` 且 parity gate=`HOLD` 时，pipeline 必须 `BLOCK`。
4. `trader_parity_gate.enabled=true` 但输入 report 缺失时，pipeline 必须 `BLOCK` 或明确失败退出，不能静默放过。

#### Trader Parity Gate

1. `allow_match_ratio` 低于阈值时失败。
2. `side_match_ratio` 低于阈值时失败。
3. `reason_match_ratio` 低于阈值时失败。
4. `exec_symbol_match_ratio` 低于阈值时失败。
5. `kernel_fills_count` 超过上限时失败。
6. `open_trigger_count > 0` 时失败。
7. `position_notional_abs_usd > 0` 时失败。
8. `identity_report.ok=false` 时失败。
9. `daily_summary_report` 既支持直接 summary，也支持 `metadata.strategy_sections` 包裹结构。

#### Session Module

1. 仅配置 `flatten_before_close_et` 时，按该值触发 EOD flatten。
2. 仅配置 `enforce_same_day_flatten=true` 时，按 `default_flatten_before_close_et` 触发。
3. 未配置任何显式字段时，不触发 flatten。
4. `policy_id/rule_engine_parity` 的取值变化不影响 EOD flatten 决策。

### 5.2 Integration Tests

1. 目标 kernel 策略启用 parity gate 时，release pipeline 产出 `GO/BLOCK` 与 parity gate 结果一致。
2. 未启用 parity gate 的其它 kernel 策略，release pipeline 行为保持不变。
3. `same-day flatten` 契约显式配置的策略，session flatten 行为不退化。
4. shadow/test parity 策略未显式配置 flatten 时，不会被自动注入 EOD flatten。

### 5.3 Regression / Safety Tests

1. 现有 `stocks_kernel` 单元测试全绿。
2. `release pipeline` 相关脚本单测全绿。
3. `session` 相关 e2e / integration 回归全绿。
4. 非目标策略默认配置冒烟通过，确认没有旁路行为变化。

建议最小回归命令：

```bash
venv/bin/pytest -q tests/unit/scripts/test_run_stocks_kernel_trader_parity_gate.py
venv/bin/pytest -q tests/unit/scripts/test_run_stocks_kernel_release_pipeline.py
venv/bin/pytest -q tests/unit/strategies/test_stocks_kernel_e2e_sessions_and_generic.py
venv/bin/pytest -q tests/unit/strategies/test_stocks_kernel_policy_and_strategy.py -k "flatten or parity or release"
```

## 6. 验收标准清单

只有下面项目全部满足，才算本次整改通过：

1. `trader_parity_gate.enabled=true` 时，release pipeline 失败不会再给出 `GO`。
2. parity gate 报告中必须包含：
   - `signal_allow_parity`
   - `signal_side_parity`
   - `signal_reason_parity`
   - `exec_symbol_parity`
   - `cadence_fill_count_guard`
   - `same_day_flatten_guard`
3. 任一关键维度失败时，`failed_checks` 必须精确列出失败项。
4. 未显式配置 `flatten_before_close_et` 或 `enforce_same_day_flatten` 的 parity/shadow 策略，不会被 session 层自动强制 EOD flatten。
5. 目标策略若要求“当日清仓”，必须通过显式配置满足，而不是依赖模块猜测。
6. 未启用 parity gate 的策略，发布路径输出与整改前保持等价。
7. 非目标策略冒烟结果不退化。

## 7. Rollout / Rollback

上线原则：

1. 先让代码支持，再按策略逐条启用 `extras.trader_parity_gate.enabled=true`
2. 不做全局默认开启
3. parity gate 先用于目标 shadow/kernel 策略，不扩大到无关策略

回滚原则：

1. 若发现误阻断，只需把目标策略的 `extras.trader_parity_gate.enabled` 置为 `false`
2. 若发现 EOD flatten 契约回归，只回滚 session 显式契约改动，不动其它发布链逻辑
3. 不允许通过修改数据库状态绕过发布链

## 8. 结论

这 3 个 finding 的本质不是“多写两个判断”，而是三条系统边界需要收紧：

1. 文档里的准入条件必须进入正式准入链。
2. 交易员语义复刻必须用完整证据判定，而不是弱化版近似。
3. 真实持仓处置必须由显式契约驱动，不能由模块猜测。

整改完成后，`stocks_kernel` 才能在“不误伤其它策略”的前提下，把 `QQQ` trader parity 作为真正可执行的发布约束。
