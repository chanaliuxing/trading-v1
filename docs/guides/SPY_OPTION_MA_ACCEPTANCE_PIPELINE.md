# SPY Option MA Acceptance Pipeline

目的：给 `spy_1dte_ma10_reject_0324_001` 提供和 `QQQ` 类似的统一证据入口，但保持它自己的 replay/shadow/cutover 规则。

## 工具

1. readiness:
- [check_spy_option_ma_readiness.py](./scripts/check_spy_option_ma_readiness.py)

2. replay parity:
- [run_spy_option_ma_parity_range.py](./scripts/run_spy_option_ma_parity_range.py)

3. live-shadow parity:
- [run_spy_option_ma_shadow_parity_day.py](./scripts/run_spy_option_ma_shadow_parity_day.py)

## 当前规则

1. active strategy config 必须满足 platform admission gate
2. rollout evidence contract 必须为绿色
3. replay parity 通过后，才能进入 live-shadow 对照
4. live-shadow parity 通过后，才能作为 cutover review 证据

## Replay 口径说明（2026-04 更新）

`run_spy_option_ma_parity_range.py` 默认会读取 active strategy (`spy_1dte_ma10_reject_0324_001`) 的 `extras`，用于减少配置偏差：

```bash
./venv/bin/python ./scripts/run_spy_option_ma_parity_range.py \
  --start 2026-03-30 \
  --end 2026-03-30 \
  --out ./runtime/state/spy_option_ma_parity_2026-03-30_active_extras.json
```

当前已知阻塞样本：

1. `2026-03-30` 在 active extras 口径下出现 `old_count=0`, `new_count=7`
2. 这说明当前主要问题不再是比对字段噪音，而是 legacy/new 触发行为差异

## 说明

`SPY` 这条链当前不是 state-shadow-first，而是 parity-first。  
也就是：

1. 先 replay parity
2. 再 live-shadow parity
3. 最后才谈 cutover review
