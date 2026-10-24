# Option Platform Coverage Report

目的：证明 option 平台不是只适配 `QQQ/SPY` 两条现网路径，而是已经能表达至少三类 definition-driven strategy archetype。  
权威工具：

- `./tool/report/build_option_platform_coverage_report.py`

## 检查目标

1. definition 总数
2. distinct `strategy_archetype` 数量
3. 每个 definition 是否有 config / registry / module identity
4. 是否达到当前阶段的最小覆盖目标：`>= 3` 个 archetype

## 当前 archetype 目标

1. `option_native_ma`
2. `stock_signal_option`
3. `generic_intraday_momentum`

第三类默认只作为 disabled sample family 存在，不进入现网 active 路径。

## 当前结果

最近一次报告结果：

1. `distinct_archetype_count = 3`
2. `coverage_goal_met = true`
3. `error_count > 0` 仍可能存在

这里的 `error_count` 不是指平台 contract 失败，而是说明 config 中仍保留了部分 legacy / standalone option path，它们没有 `plugin_cls`，因此不会被误判成 definition-driven 平台样本。

## 解读规则

1. `coverage_goal_met = true` 说明平台已经能表达至少三类 strategy archetype
2. `error_count > 0` 说明 legacy 治理尚未结束
3. 只有 `assembly-adapter report` 和 `inventory report` 同时看，才能判断 active path 是否已经合规
