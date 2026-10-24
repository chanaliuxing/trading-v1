# index_super_win vs qqq_v1 对比报告

## 结论先行
- 没有发现“大量逻辑被删除”。
- `index_super_win` 不是字节级“完全一模一样”，但核心交易决策与风控逻辑与 `qqq_v1` 保持一致。
- 主要新增是“符号映射层”和“无 short 执行腿时自动只做多”的兼容逻辑。

## 对比对象
- 旧策略: `strategies/qqq_intraday_strategy.py`
- 新策略: `strategies/index_super_win_strategy.py`
- 旧回测: `scripts/backtest_qqq_v1_morning.py`
- 新回测: `scripts/backtest_index_super_win.py`

## 代码量对比
### 策略文件
- `qqq_intraday_strategy.py`: 4648 行
- `index_super_win_strategy.py`: 4715 行
- 变化: +67 行

### 回测文件
- `backtest_qqq_v1_morning.py`: 887 行
- `backtest_index_super_win.py`: 958 行
- 变化: +71 行

## Diff 统计
### 策略文件
- `git diff --no-index --stat`: `109 insertions(+), 42 deletions(-)`
- 线级相似度 (SequenceMatcher): `0.9837`
- 匹配行数: `4605`（旧 4648 行 / 新 4715 行）
- 函数集合差异: 仅新增 `['_to_external_symbol', '_to_internal_symbol']`，无函数删除

### 回测文件
- `git diff --no-index --stat`: `89 insertions(+), 18 deletions(-)`
- 线级相似度 (SequenceMatcher): `0.9420`
- 匹配行数: `869`

## 变更性质审计
策略文件中删除/替换的内容主要是以下类型，不属于“策略逻辑删减”: 
1. 类名与文档头替换
- `QQQIntradayStrategy` -> `IndexSuperWinStrategy`

2. 硬编码符号改为映射
- 新增内部/外部符号转换方法:
  - `_to_internal_symbol`
  - `_to_external_symbol`
- 下单、持仓查询、open orders 过滤统一走外部真实交易符号。

3. 执行腿集合动态化
- 原固定 `("TQQQ", "SQQQ")` 改为基于配置 `self._active_exec_internal_symbols`。
- 相关缓存结构（5m bars、ATR、minute close）由固定双腿改为按启用腿初始化。

4. 短腿可选（仅做多降级）
- 当 `short_exec_symbol` 为空时，新增 gate 阻断 short entry。
- 实时持仓检查与风控 notional 检查对 short 侧做条件化处理。

5. 标识性字段改名
- `order_ref` 前缀从 `qqqv1:` 改为 `indexsw:`。

## “是否一模一样”判定
严格来说，不是一模一样（存在可见差异）：
- 新增符号映射层。
- 新增短腿缺失时只做多逻辑。
- 下单/持仓接口参数改为外部符号。

但在“已配置多空执行腿（如 QQQ/QQEW/TQQQ/SQQQ）”情况下：
- 核心入场判定路径、ATR/TV/高波动保护、止盈止损、回撤退出、保护止损审计等主流程保持一致。
- 未发现为实现通用化而删除核心决策模块。

## 风险提示
- `short_exec_symbol` 为空时，行为会与老策略不同（老策略固定可做空），这是设计性差异，不是回归。
- 由于新增符号映射层，若传入 symbols 配置错误，可能出现“有行情但不触发交易”的配置性问题。

## 复核建议
如需再进一步“数学级别”确认一致，可在同一交易日上做 A/B 回放：
1. 新策略配置为 `QQQ/QQEW/TQQQ/SQQQ`。
2. 同源数据、同参数跑两套。
3. 对比 entry/exit 时间、方向、触发原因、PnL 差分。
