# Option Strategy Active / Retired Inventory

目的：把 option strategy 的 operational status 固定成正式清单，而不是散落在 runbook、注释和个别测试里。  
权威来源：

1. config 实例状态：`./config.yml`
2. inventory builder：`./tool/report/build_option_strategy_inventory.py`
3. retired id 常量：`./strategies/options_kernel/inventory.py`

## 当前活动路径

截至当前仓库状态，active option strategy ids 为：

1. `qqq_intraday_option_v1_001`
2. `spy_1dte_ma10_reject_0324_001`

## 当前 retired 路径

当前明确 retired 的 strategy ids 为：

1. `qqq_0dte_opt_live_pure_001`
2. `qqq_1dte_ma10_reject_0324_v2_001`

retired 的 operational 含义：

1. config 中保持 `enabled: false`
2. 不再参与 allowlist / shared-cap / live rollout
3. 不再继续调参作为主路径

## 当前 disabled 但未声明 retired 的路径

这些路径当前是 disabled，但不自动等同于 retired：

1. `qqq_0dte_opt_live_001`
2. `qqq_0dte_opt_live_v2_001`
3. `qqq_1dte_ma10_reject_0324_001`
4. `qqq_1dte_ma10_reject_0324_pure_001`
5. `spy_1dte_ma10_reject_0324_pure_001`
6. `bull_put_spread_qqq`

这类路径的语义必须显式决定：

1. 是回放资产
2. 是预备切换路径
3. 还是应该正式加入 retired 清单

## Operator 命令

```bash
./venv/bin/python tool/report/build_option_strategy_inventory.py
```

如需落盘：

```bash
./venv/bin/python tool/report/build_option_strategy_inventory.py \
  --output runtime/state/option_strategy_inventory.json
```
