# HPO 框架 - 如何添加新策略

**版本**: v1.0
**日期**: 2025-10-26
**适用范围**: 重构后的 HPO 框架

---

## 📋 概述

重构后的 HPO 框架通过**策略适配器模式**实现了策略无关设计。添加新策略只需 3 个步骤：

1. ✅ 创建策略适配器（100-300 行代码）
2. ✅ 注册到 `StrategyRegistry`
3. ✅ 创建配置文件

**无需修改框架核心代码！**

---

## 🚀 快速开始

### Step 1: 创建适配器文件

创建 `adapters/my_strategy_adapter.py`：

```python
# -*- coding: utf-8 -*-
"""
My Strategy Adapter
-------------------
你的策略的 HPO 适配器

Author: Your Name
Date: 2025-10-26
"""

from __future__ import annotations
from typing import Dict, Any, List
import pandas as pd
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategy_adapter import StrategyAdapter


class MyStrategyAdapter(StrategyAdapter):
    """你的策略的适配器"""

    def load_data(
        self,
        symbols: List[str],
        start: str,
        end: str,
        clickhouse_config: Dict[str, Any],
        **kwargs
    ) -> pd.DataFrame:
        """
        加载策略需要的数据

        Args:
            symbols: 股票代码列表，如 ["TSLA", "AAPL"]
            start: 开始时间 "YYYY-MM-DD HH:MM:SS"
            end: 结束时间
            clickhouse_config: {"host": "localhost", "port": 9000, ...}
            **kwargs: 额外参数，如 features_table, timeframe

        Returns:
            DataFrame 包含策略需要的所有列
        """
        from clickhouse_driver import Client

        features_table = kwargs.get("features_table", "trading.features_1m")
        client = Client(**clickhouse_config)
        syms = ",".join([f"'{s}'" for s in symbols])

        # 根据你的策略需求编写 SQL
        sql = f"""
        SELECT
            ts,
            symbol,
            open,
            high,
            low,
            close,
            volume,
            -- 添加你需要的其他列
            vwap,
            atr_14,
            rsi_14
        FROM {features_table}
        WHERE symbol IN ({syms})
          AND ts >= toDateTime('{start}')
          AND ts < toDateTime('{end}')
        ORDER BY ts, symbol
        """

        data = client.execute(sql)
        columns = ["ts", "symbol", "open", "high", "low", "close", "volume",
                   "vwap", "atr_14", "rsi_14"]

        return pd.DataFrame(data, columns=columns)

    def validate_data(self, df: pd.DataFrame) -> None:
        """
        验证数据是否满足策略要求
        """
        required = ["ts", "symbol", "close", "volume"]
        missing = set(required) - set(df.columns)

        if missing:
            raise ValueError(
                f"策略缺少必需列: {missing}\n"
                f"当前列: {list(df.columns)}"
            )

        if df.empty:
            raise ValueError("数据为空，请检查时间范围和股票代码")

    def parse_params(
        self,
        grid_params: Dict[str, Any],
        base_extras: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        解析参数结构

        根据你的策略参数结构选择：

        1. 扁平参数（如 TSLA V7_2）:
           grid_params = {"rsi_period": 14, "bb_std": 2.0}
           → {"extras": {"strategy_config": {"rsi_period": 14, "bb_std": 2.0}}}

        2. 嵌套参数（如 RevE）:
           grid_params = {"strategy_A.param1": 0.5}
           → {"extras": {"strategy_A": {"param1": 0.5}}}
        """
        import copy
        result = copy.deepcopy(base_extras)

        # 选项 1: 扁平参数（简单合并）
        # result.update(grid_params)

        # 选项 2: 嵌套参数（点分隔路径解析）
        for key, value in grid_params.items():
            if "." in key:
                # 解析 "strategy_A.param1" → strategy_A["param1"]
                parts = key.split(".")
                target = result
                for part in parts[:-1]:
                    if part not in target:
                        target[part] = {}
                    target = target[part]
                target[parts[-1]] = value
            else:
                result[key] = value

        return {"extras": result}

    def get_required_columns(self) -> List[str]:
        """返回策略必需的列"""
        return ["ts", "symbol", "open", "high", "low", "close", "volume"]

    def supports_ml_signals(self) -> bool:
        """
        策略是否支持 ML 信号

        - True: 框架会调用 _prepare_model_data() 生成 ml_signal, ml_conf 列
        - False: 纯规则策略，跳过 ML 流程
        """
        return False  # 根据你的策略修改

    def get_default_params(self) -> Dict[str, Any]:
        """返回策略的默认参数（用于文档和测试）"""
        return {
            "my_param1": 10,
            "my_param2": 0.5
        }

    def get_data_sources(self) -> List[str]:
        """返回策略需要的数据源（用于文档）"""
        return ["trading.features_1m"]

    def get_strategy_description(self) -> str:
        """返回策略描述"""
        return "My custom trading strategy"
```

---

### Step 2: 注册适配器

编辑 `strategy_registry.py`，在 `_register_builtin_adapters()` 函数中添加：

```python
def _register_builtin_adapters():
    """注册内置的策略适配器"""
    # ... 现有代码 ...

    try:
        # 添加你的适配器
        from adapters.my_strategy_adapter import MyStrategyAdapter
        StrategyRegistry.register(
            "strategies.my_strategy.MyStrategy",  # 你的策略类路径
            MyStrategyAdapter
        )
    except ImportError:
        pass  # 适配器未实现，跳过
```

**验证注册**:

```bash
python3 -c "from strategy_registry import StrategyRegistry; print(StrategyRegistry.list_strategies())"
```

应该看到你的策略出现在列表中。

---

### Step 3: 创建配置文件

创建 `config/hpo_my_strategy.json`：

```json
{
  "hpo": {
    "strategy_path": "strategies.my_strategy.MyStrategy",
    "symbols": ["TSLA", "AAPL"],
    "start": "2025-07-01 09:30:00",
    "end": "2025-09-30 16:00:00",
    "interval_sec": 60,
    "timeframe": "1m",
    "features_table": "trading.features_1m",

    "clickhouse": {
      "host": "localhost",
      "port": 9000,
      "user": "default",
      "password": "",
      "database": "trading"
    },

    "strategy_base_extras": {
      "my_config": {
        "universe": ["TSLA", "AAPL"],
        "risk_per_trade": 0.02
      }
    },

    "grid": {
      "my_param1": [10, 20, 30],
      "my_param2": [0.3, 0.5, 0.7]
    },

    "fold_generator": {
      "start": "2025-07-01 09:30:00",
      "end": "2025-09-30 16:00:00",
      "is_days": 30,
      "oos_days": 7,
      "gap_days": 0,
      "step_days": 7
    },

    "broker": {
      "initial_cash": 100000.0,
      "commission_per_share": 0.005,
      "commission_min": 1.0,
      "slippage_bps": 1.0,
      "fill_ref": "open"
    },

    "objective": {
      "metric": "sharpe",
      "maximize": true,
      "min_trades": 10
    },

    "parallel": {
      "workers": 4
    },

    "out_dir": "alpha_out/my_strategy"
  }
}
```

---

### Step 4: 运行 HPO

```bash
python alpha_loop.py \
  --config config/hpo_my_strategy.json \
  --provider ml \
  --gens 5 \
  --out_dir alpha_out/my_strategy \
  --k-top 10
```

---

## 📚 详细示例

### 示例 1: 简单的均线策略

```python
class MAStrategyAdapter(StrategyAdapter):
    """移动平均策略适配器"""

    def load_data(self, symbols, start, end, clickhouse_config, **kwargs):
        from clickhouse_driver import Client
        client = Client(**clickhouse_config)
        syms = ",".join([f"'{s}'" for s in symbols])

        sql = f"""
        SELECT ts, symbol, open, high, low, close, volume,
               ema_20, ema_50
        FROM trading.features_1m
        WHERE symbol IN ({syms})
          AND ts >= toDateTime('{start}')
          AND ts < toDateTime('{end}')
        ORDER BY ts, symbol
        """

        data = client.execute(sql)
        return pd.DataFrame(data, columns=[
            "ts", "symbol", "open", "high", "low", "close", "volume",
            "ema_20", "ema_50"
        ])

    def validate_data(self, df):
        required = ["ts", "symbol", "close", "ema_20", "ema_50"]
        missing = set(required) - set(df.columns)
        if missing:
            raise ValueError(f"缺少列: {missing}")

    def parse_params(self, grid_params, base_extras):
        # 简单合并
        return {"extras": {**base_extras, **grid_params}}

    def get_required_columns(self):
        return ["ts", "symbol", "close", "ema_20", "ema_50"]

    def supports_ml_signals(self):
        return False  # 纯规则策略
```

**配置文件**:

```json
{
  "hpo": {
    "strategy_path": "strategies.ma_strategy.MAStrategy",
    "grid": {
      "fast_period": [10, 20],
      "slow_period": [30, 50]
    }
  }
}
```

---

### 示例 2: 带 ML 增强的策略

```python
class MLEnhancedStrategyAdapter(StrategyAdapter):
    """ML 增强策略适配器"""

    def load_data(self, symbols, start, end, clickhouse_config, **kwargs):
        # 加载数据（包含特征）
        # ...
        pass

    def validate_data(self, df):
        # 验证数据
        pass

    def parse_params(self, grid_params, base_extras):
        # 解析参数
        return {"extras": {**base_extras, **grid_params}}

    def get_required_columns(self):
        return ["ts", "symbol", "close", "volume", "feature1", "feature2"]

    def supports_ml_signals(self):
        return True  # 支持 ML 增强 ⭐
```

**配置文件**:

```json
{
  "hpo": {
    "strategy_path": "strategies.ml_strategy.MLStrategy",
    "grid": {
      "model_type": ["xgb"],
      "threshold": [0.5, 0.7]
    },
    "model_target_col": "y",
    "model_task": "clf"
  }
}
```

---

## 🔧 参数解析模式

### 模式 1: 扁平参数（推荐用于简单策略）

**Grid**:
```python
{"rsi_period": 14, "bb_std": 2.0}
```

**解析代码**:
```python
def parse_params(self, grid_params, base_extras):
    return {"extras": {**base_extras, **grid_params}}
```

**输出**:
```python
{
    "extras": {
        "rsi_period": 14,
        "bb_std": 2.0
    }
}
```

---

### 模式 2: 嵌套参数（推荐用于复杂策略）

**Grid**:
```python
{"strategy_A.near_vwap": 0.20, "strategy_A.sl_atr": 0.50}
```

**解析代码**:
```python
def parse_params(self, grid_params, base_extras):
    import copy
    result = copy.deepcopy(base_extras)

    for key, value in grid_params.items():
        if "." in key:
            parts = key.split(".")
            target = result
            for part in parts[:-1]:
                if part not in target:
                    target[part] = {}
                target = target[part]
            target[parts[-1]] = value
        else:
            result[key] = value

    return {"extras": result}
```

**输出**:
```python
{
    "extras": {
        "strategy_A": {
            "near_vwap": 0.20,
            "sl_atr": 0.50
        }
    }
}
```

---

### 模式 3: 向后兼容（用于迁移旧策略）

**Grid**:
```python
{"rsi_period": 14}
```

**解析代码（保持 strategy_config 包装器）**:
```python
def parse_params(self, grid_params, base_extras):
    import copy
    result = copy.deepcopy(base_extras)

    if "strategy_config" not in result:
        result["strategy_config"] = {}

    result["strategy_config"].update(grid_params)

    return {"extras": result}
```

**输出**:
```python
{
    "extras": {
        "strategy_config": {
            "position_size": 0.10,
            "rsi_period": 14
        }
    }
}
```

---

## 📖 适配器接口完整说明

### 必需方法

| 方法 | 作用 | 返回类型 |
|------|------|---------|
| `load_data()` | 加载策略需要的数据 | `pd.DataFrame` |
| `validate_data()` | 验证数据完整性 | `None` (抛出异常) |
| `parse_params()` | 解析参数结构 | `Dict[str, Any]` |
| `get_required_columns()` | 返回必需列列表 | `List[str]` |

### 可选方法

| 方法 | 默认值 | 作用 |
|------|--------|------|
| `supports_ml_signals()` | `False` | 是否支持 ML 增强 |
| `get_default_params()` | `{}` | 默认参数（文档用） |
| `get_data_sources()` | `[]` | 数据源列表（文档用） |
| `get_strategy_description()` | `""` | 策略描述（文档用） |

---

## 🧪 测试适配器

### 单元测试模板

在适配器文件末尾添加：

```python
if __name__ == "__main__":
    print("=" * 60)
    print("My Strategy Adapter Test")
    print("=" * 60)

    adapter = MyStrategyAdapter()

    # 测试 1: 参数解析
    print("\n测试 1: 参数解析")
    result = adapter.parse_params(
        {"my_param1": 10},
        {"base_config": {"key": "value"}}
    )
    print(f"结果: {result}")

    # 测试 2: 必需列
    print("\n测试 2: 必需列")
    print(f"必需列: {adapter.get_required_columns()}")

    # 测试 3: ML 支持
    print("\n测试 3: ML 支持")
    print(f"支持 ML: {adapter.supports_ml_signals()}")

    print("\n✅ 所有测试通过")
```

**运行测试**:

```bash
python adapters/my_strategy_adapter.py
```

---

### 集成测试

```bash
# 测试适配器是否正确注册
python3 -c "
from strategy_registry import StrategyRegistry
adapter = StrategyRegistry.get_adapter('strategies.my_strategy.MyStrategy')
print(f'✅ 适配器加载成功: {type(adapter).__name__}')
print(f'   支持 ML: {adapter.supports_ml_signals()}')
print(f'   必需列: {adapter.get_required_columns()}')
"
```

---

## 🐛 常见问题

### Q1: 适配器未注册？

**症状**: `ValueError: 未找到策略 'strategies.my_strategy.MyStrategy' 的适配器`

**解决**:
1. 检查 `strategy_registry.py` 中是否添加了注册代码
2. 确认 `from adapters.my_strategy_adapter import MyStrategyAdapter` 路径正确
3. 确认适配器类名正确

```bash
# 验证注册
python3 -c "from strategy_registry import StrategyRegistry; print(StrategyRegistry.list_strategies())"
```

---

### Q2: 数据加载失败？

**症状**: `ValueError: 从 ClickHouse 加载数据失败`

**解决**:
1. 检查 SQL 语句中的列名是否存在
2. 验证表名是否正确
3. 检查时间范围是否有数据

```bash
# 验证表 schema
clickhouse-client --query "DESCRIBE trading.features_1m" | head -20

# 验证数据时间范围
clickhouse-client --query "SELECT min(ts), max(ts) FROM trading.features_1m WHERE symbol='TSLA'"
```

---

### Q3: 参数解析不正确？

**症状**: 策略收到的参数结构与预期不符

**调试**:
1. 在适配器的 `parse_params()` 中添加打印:
```python
def parse_params(self, grid_params, base_extras):
    print(f"[DEBUG] grid_params: {grid_params}")
    print(f"[DEBUG] base_extras: {base_extras}")
    result = ...
    print(f"[DEBUG] result: {result}")
    return result
```

2. 运行小规模测试验证参数传递

---

### Q4: ML 集成不工作？

**症状**: 策略支持 ML 但未生成 ml_signal 列

**检查**:
1. `supports_ml_signals()` 返回 `True`
2. 配置文件包含 `model_target_col`, `model_task`
3. DataFrame 包含特征列和目标列

---

## 📁 文件组织

```
trading/
├── adapters/
│   ├── __init__.py
│   ├── reve_adapter.py              # RevE 示例
│   ├── tsla_v7_2_adapter.py         # TSLA V7_2 示例
│   └── my_strategy_adapter.py       # 你的适配器 ⭐
│
├── strategies/
│   └── my_strategy.py               # 你的策略实现
│
├── config/
│   └── hpo_my_strategy.json         # HPO 配置 ⭐
│
├── strategy_registry.py              # 注册表（需修改）
└── wf_hpo.py                        # HPO 框架（无需修改）
```

---

## 🎯 最佳实践

### 1. 适配器命名

- ✅ `MyStrategyAdapter` (策略名 + Adapter)
- ❌ `MyAdapter`, `Strategy1`

### 2. 数据加载

- ✅ 只加载策略需要的列（避免浪费）
- ✅ 使用参数化查询（避免 SQL 注入）
- ✅ 添加异常处理和错误信息

### 3. 参数解析

- ✅ 使用 `copy.deepcopy()` 避免修改原始配置
- ✅ 支持点分隔路径（如 `"strategy_A.param1"`）
- ✅ 保持向后兼容

### 4. 文档

- ✅ 添加 docstring 说明参数格式
- ✅ 提供默认参数示例
- ✅ 添加单元测试

---

## 📖 参考文档

- [HPO 框架重构分析](HPO_FRAMEWORK_REFACTORING_ANALYSIS.md)
- [重构执行总结](HPO_REFACTORING_SUMMARY.md)
- [策略适配器基类](../strategy_adapter.py)
- [RevE 适配器示例](../adapters/reve_adapter.py)
- [TSLA V7_2 适配器示例](../adapters/tsla_v7_2_adapter.py)

---

## 🆘 获取帮助

遇到问题？

1. 查看现有适配器示例 (`adapters/reve_adapter.py`, `adapters/tsla_v7_2_adapter.py`)
2. 运行测试验证注册: `python3 -c "from strategy_registry import StrategyRegistry; print(StrategyRegistry.list_strategies())"`
3. 检查日志输出: `tail -f /workspace/grafana-stack/logs/trader.log`

---

**文档版本**: v1.0
**最后更新**: 2025-10-26
**维护者**: Trading Team
