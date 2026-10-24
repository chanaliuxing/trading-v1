# Option Strategy Risk Matrix

目的：把 option 平台常见变更类型和对应风险固定下来，减少“看起来只是改个参数，实际上改了策略语义”的情况。

## 1. 低风险

1. 调整 `shared_cap_group_limit_usd`
2. 调整实例级 entry time window
3. 调整 `enable_live_parity_snapshot`
4. 调整 `live_parity_snapshot_root`

要求：

1. config contract 仍通过
2. 不改变 definition identity

## 2. 中风险

1. 调整 `contracts`
2. 调整 `dte_preferred / dte_fallback_max`
3. 调整 `delta_*`
4. 调整 `spread_max_pct`
5. 调整 `force_flatten_time`

要求：

1. 需要 replay
2. 建议 shadow

## 3. 高风险

1. 切换 `definition_id`
2. 切换 `signal_module`
3. 切换 `risk_policy`
4. 切换 `profit_management_policy`
5. 切换 `flatten_policy`
6. 改 `ownership_scope`
7. 改 `kernel_mode`

要求：

1. replay 必须通过
2. shadow 必须通过
3. admission gate 需要重新检查

## 4. 禁止项

1. 绕过 definition contract 修改 protected extras
2. active strategy 关闭 `strict_scope_validation`
3. 未经治理直接把 legacy runtime 当成 definition-driven 样本
