# Option Trader-Facing Schema

目的：把交易员真正需要配置的 option strategy 实例字段固定下来，避免继续靠复制策略类或口头约定上线。  
权威 schema：

- [option_strategy_instance.v1.schema.json](./schema/strategy_engine/option_strategy_instance.v1.schema.json)

## 1. 第一性原理

交易员配置的是：

1. 用哪套 definition
2. 跑哪个 underlying
3. 用哪类 risk symbol
4. 归属到哪个 capital / ownership scope
5. 用什么实例级窗口和风控参数

交易员不配置的是：

1. kernel 主流程
2. 私有 signal 代码
3. 私有 flatten 状态机
4. 绕过 definition contract 的 override

## 2. 必填字段

顶层：

1. `id`
2. `enabled`
3. `module`
4. `class`
5. `params.extras`

`extras` 最小必填：

1. `definition_id`
2. `underlying`
3. `risk_symbol`
4. `kernel_mode`
5. `strict_scope_validation`
6. `shared_cap_group_id`
7. `shared_cap_group_strategy_ids`

## 3. 字段分层

固定 contract 字段：

1. `definition_id`
2. `underlying`
3. `risk_symbol`
4. `kernel_mode`
5. `strict_scope_validation`

ownership / capital 字段：

1. `shared_cap_group_id`
2. `shared_cap_group_limit_usd`
3. `shared_cap_group_strategy_ids`

实例级可调字段：

1. `contracts`
2. `entry_start_am`
3. `entry_end_am`
4. `entry_start_pm`
5. `entry_end_pm`
6. `no_new_entry_after`
7. `force_flatten_time`
8. `dte_preferred`
9. `dte_fallback_max`
10. `delta_min`
11. `delta_max`
12. `delta_fallback_min`
13. `delta_fallback_max`
14. `spread_max_pct`
15. `min_open_interest`

observability 字段：

1. `enable_live_parity_snapshot`
2. `enable_live_shadow_mode`
3. `live_parity_profile_id`
4. `live_parity_snapshot_root`
5. `market_data_profile_id`

## 4. 使用规则

1. `definition_id` 必须对应 definition registry 中已注册对象
2. `underlying` / `risk_symbol` 不能与 definition 解析结果漂移
3. `kernel_mode` 只能是 `plugin / pure_plugin / hybrid`
4. `strict_scope_validation` 必须是 `true`
5. `shared_cap_group_strategy_ids` 必须包含当前实例自身 id

## 5. 样板

示例模板：

- [definition_driven_strategy.example.yml](./configs/option_strategy/definition_driven_strategy.example.yml)
