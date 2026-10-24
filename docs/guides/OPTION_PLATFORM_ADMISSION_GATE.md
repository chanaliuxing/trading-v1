# Option Platform Admission Gate

目的：统一回答“当前 option 平台是否已经达到进入平台级 review 的最低治理标准”。  
权威工具：

- [build_option_platform_admission_gate.py](./tool/report/build_option_platform_admission_gate.py)
- [run_option_platform_admission_gate.py](./tool/replay/run_option_platform_admission_gate.py)

## 检查项

1. inventory 中 active option strategy 数量
2. active path 是否全部通过 assembly-adapter compliance
3. 平台 coverage 是否仍保持 `>= 3` archetype
4. active strategy config 是否满足 trader-facing config contract
5. 是否还存在 legacy active option path

## 通过条件

1. `assembly_adapter_fail_count = 0`
2. `active_validation_fail_count = 0`
3. `legacy_active_count = 0`
4. `coverage_goal_met = true`

满足以上条件后，报告会输出：

1. `recommendation = PLATFORM_ADMISSION_REVIEW_ALLOWED`
2. `platform_admission_blocked = false`

## 不代表的事情

这个 gate 只回答“平台治理与接入 contract 是否达标”。  
它不等于：

1. 某个具体 option family 已完成 replay
2. 某个具体 strategy 已完成 shadow acceptance
3. 可以直接 live cutover

## 运行方式

```bash
./venv/bin/python ./tool/replay/run_option_platform_admission_gate.py
```
