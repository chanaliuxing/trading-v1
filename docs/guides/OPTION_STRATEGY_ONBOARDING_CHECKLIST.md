# Option Strategy Onboarding Checklist

目的：定义一个新 option strategy 从 definition 到 shadow 再到 live review 的统一准入清单。

## 1. Definition 准入

1. 已在 definition registry 注册
2. `strategy_family` 明确
3. `strategy_archetype` 明确
4. `signal_module / direction_mapping / contract / execution / position / risk / profit / flatten` 全部有 identity
5. `ownership_scope` 明确

## 2. Config 准入

1. 配置满足 [option_strategy_instance.v1.schema.json](./schema/strategy_engine/option_strategy_instance.v1.schema.json)
2. `shared_cap_group_strategy_ids` 包含自身
3. `strict_scope_validation = true`
4. `kernel_mode` 属于 plugin family
5. `definition_id / underlying / risk_symbol` 与 plugin 解析结果一致

## 3. 平台治理准入

1. inventory 可见
2. assembly-adapter report 通过
3. platform coverage report 不退化
4. platform admission gate 不出现 active blocker

## 4. 策略级验证准入

1. replay 可运行
2. shadow 审计链可运行
3. readiness / acceptance / cutover 工具可出结论
4. rollback 路径清晰

## 5. 不允许上线的情况

1. 复制 legacy strategy class 但没有 definition contract
2. plugin 自己携带第二套 signal / flatten / risk 逻辑
3. ownership scope 不清晰
4. 缺少 inventory / assembly / admission 证据
