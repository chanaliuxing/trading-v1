# HPO 框架通用化重构分析

## 🎯 目标
将当前绑定 TSLA V7_2 策略的 HPO 框架重构为**通用的、可插拔的策略优化平台**。

---

## 🔍 第一部分：硬编码问题诊断

### 1. 策略路径硬编码

**位置**:
- `wf_hpo.py:316` - `strategy_path = cfg.get("strategy_path", "tsla_v7_2.TSLASimplifiedV7_2")`
- `backtest_ml_engine.py:75` - `strategy_path: str = "tsla_v7_2.TSLASimplifiedV7_2"`

**问题**: 默认值硬编码为 TSLA 策略，导致配置错误时静默使用错误策略

**影响**:
- 运行 RevE 策略时，如果配置文件缺少 `strategy_path`，会静默加载 TSLA 策略
- 错误难以发现，因为不会报错，只是结果错误

---

### 2. 数据加载机制硬编码

#### 2.1 数据来源假设

**位置**: `wf_hpo.py:319-327`

```python
dataset_df = cfg.get("dataset_df")
if dataset_df is not None:
    df = pd.DataFrame(dataset_df)
    if not all(c in df.columns for c in ["ts", "symbol"]):
        raise ValueError("dataset_df must contain 'ts' and 'symbol' columns")
    self.dataset_df = df.sort_values(["ts", "symbol"]).reset_index(drop=True)
else:
    self.dataset_df = None
```

**问题**:
1. **只支持预加载模式**: 假设所有数据必须提前加载到内存的 `dataset_df`
2. **缺少策略自定义数据加载**: 不同策略可能需要不同的数据源
   - TSLA V7_2: `trading.features_1m` (单表)
   - RevE: `trading.features_1m` + `trading.bars_5m` + SFS Redis (多源)
3. **列名假设**: 只检查 `ts`, `symbol`，但不检查策略实际需要的列

**影响**:
- RevE 策略无法使用，因为它需要动态查询 ClickHouse 和 Redis
- 无法支持流式数据或实时查询
- 内存占用大（必须预加载所有数据）

#### 2.2 数据列假设

**位置**: `backtest_ml_engine.py:302-313`

```python
needed = {"ts","symbol","open","high","low","close"}
missing = needed - set(out.columns)
if missing:
    raise ValueError(f"Missing required columns: {missing}")

# Ensure volume exists (default 0 if missing)
if "volume" not in out.columns:
    out["volume"] = 0.0
```

**问题**:
- 硬编码了必需列：`ts, symbol, open, high, low, close`
- 不同策略需要不同的列：
  - TSLA V7_2: 基础 OHLCV
  - RevE: OHLCV + `vwap` + `atr_14` + `ema_20` + ...

**影响**:
- 策略必须适配框架的列名约定
- 无法支持自定义列名（如 `price` vs `close`）

---

### 3. 参数传递机制硬编码

#### 3.1 扁平化参数结构

**位置**: `wf_hpo.py:466-478`

```python
extras = {
    **self.strategy_base_extras,
    "strategy_config": {
        **self.strategy_base_extras.get("strategy_config", {}),
        **params
    }
}
```

**问题**:
1. **假设参数是扁平字典**: `params = {"rsi_period": 14, "bb_std": 2.0}`
2. **不支持嵌套结构**: RevE 需要 `{"risk": {"per_trade_pct": 0.0025}, "schedule": {...}}`
3. **参数合并逻辑固定**: 所有参数都合并到 `strategy_config` 键下

**影响**:
- RevE 的嵌套配置无法正确传递
- `grid["strategy_A.near_vwap_atr"]` 这种点分隔命名无法解析
- 策略必须适配框架的参数结构

#### 3.2 Grid 参数命名假设

**位置**: `wf_hpo.py:60-66`

```python
def _product_dict(grid: Dict[str, Iterable[Any]]) -> List[Dict[str, Any]]:
    keys = list(grid.keys())
    vals = [list(v) for v in grid.values()]
    combos = []
    for tup in itertools.product(*vals):
        combos.append({k: v for k, v in zip(keys, tup)})
    return combos
```

**问题**:
- 将 `grid` 的 key 直接作为参数名
- 不支持嵌套路径解析，如 `"risk.per_trade_pct"` → `params["risk"]["per_trade_pct"]`

---

### 4. ML 模型集成硬编码

**位置**: `wf_hpo.py:422-511` - `_prepare_model_data()`

**问题**:
1. **假设数据集包含所有特征**: `feature_cols = [c for c in df_is.columns if c not in exclude]`
2. **假设有 `target_col` 列**: 用于训练 ML 模型
3. **假设策略接受 `ml_pred`, `ml_signal`, `ml_conf` 列**

**影响**:
- 只适用于支持 ML 信号的策略
- RevE 等纯规则策略无法使用（它不需要 ML 列）
- 必须预先准备好训练标签

---

### 5. ClickHouse 查询硬编码

**位置**: `backtest_ml_engine.py:250-288`

```python
def _clickhouse_query(self) -> Optional[pd.DataFrame]:
    # ...
    if self.cfg.bars_table:
        sql = f"""
            SELECT ts, symbol, open, high, low, close, volume,
                   0 as vwap  -- 💀 硬编码 vwap=0
            FROM {tbl}
            ...
        """
```

**问题**:
- SQL 查询硬编码了列名和默认值
- `vwap` 默认为 0（如果表中没有）
- 不支持策略自定义 SQL 查询

---

## 🏗️ 第二部分：通用化重构方案

### 设计原则

1. **策略无关**: 框架不应假设任何特定策略的数据或参数结构
2. **可插拔**: 策略应提供适配器（Adapter）来定义自己的数据加载和参数解析
3. **向后兼容**: 现有 TSLA V7_2 策略应不需修改即可运行
4. **扩展性**: 新策略只需实现适配器接口，无需修改框架代码

---

### 核心抽象层设计

#### 1. 策略适配器接口

```python
# strategy_adapter.py
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import pandas as pd

class StrategyAdapter(ABC):
    """
    策略适配器基类 - 每个策略实现自己的适配器

    职责:
    1. 定义数据加载逻辑
    2. 定义参数结构和解析逻辑
    3. 定义数据验证规则
    """

    @abstractmethod
    def load_data(
        self,
        symbols: List[str],
        start: str,
        end: str,
        clickhouse_config: Dict[str, Any],
        **kwargs
    ) -> pd.DataFrame:
        """
        加载策略所需的数据

        返回的 DataFrame 必须包含策略需要的所有列
        框架不做任何假设，由策略自己决定
        """
        pass

    @abstractmethod
    def validate_data(self, df: pd.DataFrame) -> None:
        """
        验证数据是否满足策略要求

        抛出 ValueError 如果数据不符合要求
        """
        pass

    @abstractmethod
    def parse_params(
        self,
        grid_params: Dict[str, Any],
        base_extras: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        解析和合并参数

        Args:
            grid_params: 从 grid 生成的参数组合
            base_extras: 基础配置（strategy_base_extras）

        Returns:
            策略最终的 params 字典
        """
        pass

    @abstractmethod
    def get_required_columns(self) -> List[str]:
        """
        返回策略必需的数据列
        """
        pass

    def supports_ml_signals(self) -> bool:
        """
        策略是否支持 ML 信号增强

        如果返回 True，框架会调用 _prepare_model_data()
        """
        return False

    def get_default_params(self) -> Dict[str, Any]:
        """
        返回策略的默认参数
        """
        return {}
```

---

#### 2. TSLA V7_2 适配器实现

```python
# adapters/tsla_v7_2_adapter.py
from strategy_adapter import StrategyAdapter
import pandas as pd

class TSLAV72Adapter(StrategyAdapter):
    """TSLA V7_2 策略的适配器"""

    def load_data(
        self,
        symbols: List[str],
        start: str,
        end: str,
        clickhouse_config: Dict[str, Any],
        **kwargs
    ) -> pd.DataFrame:
        """
        从 ClickHouse 加载 features_1m 表
        """
        from clickhouse_driver import Client

        client = Client(**clickhouse_config)
        features_table = kwargs.get("features_table", "trading.features_1m")
        syms = ",".join([f"'{s}'" for s in symbols])

        sql = f"""
        SELECT ts, symbol, open, high, low, close, volume,
               atr_14, ema_20, rsi_14, z_price_20, sma_20, std_20
        FROM {features_table}
        WHERE symbol IN ({syms})
          AND ts >= toDateTime('{start}')
          AND ts < toDateTime('{end}')
          AND toHour(ts) >= 9 AND toHour(ts) < 16
        ORDER BY ts, symbol
        """

        data = client.execute(sql)
        cols = ["ts", "symbol", "open", "high", "low", "close", "volume",
                "atr_14", "ema_20", "rsi_14", "z_price_20", "sma_20", "std_20"]

        df = pd.DataFrame(data, columns=cols)
        return df

    def validate_data(self, df: pd.DataFrame) -> None:
        """验证必需列"""
        required = ["ts", "symbol", "open", "high", "low", "close", "volume"]
        missing = set(required) - set(df.columns)
        if missing:
            raise ValueError(f"TSLA V7_2 策略缺少必需列: {missing}")

    def parse_params(
        self,
        grid_params: Dict[str, Any],
        base_extras: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        TSLA V7_2 使用扁平参数结构

        grid_params: {"rsi_period": 14, "bb_std": 2.0}
        base_extras: {"tsla_v7_2_config": {...}}

        返回: {"extras": {"tsla_v7_2_config": {...}, "rsi_period": 14, ...}}
        """
        return {
            "extras": {
                **base_extras,
                **grid_params
            }
        }

    def get_required_columns(self) -> List[str]:
        return ["ts", "symbol", "open", "high", "low", "close", "volume",
                "atr_14", "ema_20", "rsi_14"]

    def supports_ml_signals(self) -> bool:
        return True  # TSLA V7_2 支持 ML 信号

    def get_default_params(self) -> Dict[str, Any]:
        return {
            "rsi_period": 14,
            "bb_std": 2.0,
            "atr_period": 14
        }
```

---

#### 3. RevE 策略适配器实现

```python
# adapters/reve_adapter.py
from strategy_adapter import StrategyAdapter
import pandas as pd

class RevEAdapter(StrategyAdapter):
    """RevE Intraday 策略的适配器"""

    def load_data(
        self,
        symbols: List[str],
        start: str,
        end: str,
        clickhouse_config: Dict[str, Any],
        **kwargs
    ) -> pd.DataFrame:
        """
        RevE 需要多个数据源:
        1. features_1m - 1分钟技术指标
        2. bars_5m - 5分钟 K线（用于 ORB 策略）

        这里简化为只加载 features_1m，实际可以加载多个表并合并
        """
        from clickhouse_driver import Client

        client = Client(**clickhouse_config)
        features_table = kwargs.get("features_table", "trading.features_1m")
        syms = ",".join([f"'{s}'" for s in symbols])

        # 加载更多列（RevE 需要）
        sql = f"""
        SELECT ts, symbol, open, high, low, close, volume,
               vwap, atr_14, ema_20, rsi_14, z_price_20, z_vol_20
        FROM {features_table}
        WHERE symbol IN ({syms})
          AND ts >= toDateTime('{start}')
          AND ts < toDateTime('{end}')
        ORDER BY ts, symbol
        """

        data = client.execute(sql)
        cols = ["ts", "symbol", "open", "high", "low", "close", "volume",
                "vwap", "atr_14", "ema_20", "rsi_14", "z_price_20", "z_vol_20"]

        df = pd.DataFrame(data, columns=cols)

        # RevE 策略可以在这里添加 5分钟数据的加载和合并逻辑
        # df_5m = self._load_5m_bars(client, symbols, start, end)
        # df = pd.merge(df, df_5m, ...)

        return df

    def validate_data(self, df: pd.DataFrame) -> None:
        """RevE 需要额外的列"""
        required = ["ts", "symbol", "open", "high", "low", "close", "volume",
                    "vwap", "atr_14"]
        missing = set(required) - set(df.columns)
        if missing:
            raise ValueError(f"RevE 策略缺少必需列: {missing}")

    def parse_params(
        self,
        grid_params: Dict[str, Any],
        base_extras: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        RevE 使用嵌套参数结构 + 点分隔命名

        grid_params: {
            "strategy_A.near_vwap_atr": 0.20,
            "strategy_A.sl_atr": 0.50,
            "strat_B_sl_atr5m": 0.40
        }

        base_extras: {
            "universe": ["TSLA", "NVDA"],
            "risk": {"per_trade_pct": 0.0025, ...},
            "schedule": {...}
        }

        需要解析点分隔路径并合并到正确的嵌套结构
        """
        # 深拷贝 base_extras
        import copy
        result = copy.deepcopy(base_extras)

        # 解析 grid_params 中的点分隔路径
        for key, value in grid_params.items():
            if "." in key:
                # 解析嵌套路径: "strategy_A.near_vwap_atr" → ["strategy_A", "near_vwap_atr"]
                parts = key.split(".")
                target = result
                for part in parts[:-1]:
                    if part not in target:
                        target[part] = {}
                    target = target[part]
                target[parts[-1]] = value
            else:
                # 扁平参数直接添加
                result[key] = value

        return {"extras": result}

    def get_required_columns(self) -> List[str]:
        return ["ts", "symbol", "open", "high", "low", "close", "volume",
                "vwap", "atr_14", "ema_20"]

    def supports_ml_signals(self) -> bool:
        return False  # RevE 目前是纯规则策略

    def get_default_params(self) -> Dict[str, Any]:
        return {
            "strategy_A": {
                "near_vwap_atr": 0.20,
                "sl_atr": 0.50,
                "tp1_atr": 1.00
            },
            "risk": {
                "per_trade_pct": 0.0025
            }
        }
```

---

#### 4. 适配器注册表

```python
# strategy_registry.py
from typing import Dict, Type
from strategy_adapter import StrategyAdapter

class StrategyRegistry:
    """策略适配器注册表"""

    _adapters: Dict[str, Type[StrategyAdapter]] = {}

    @classmethod
    def register(cls, strategy_path: str, adapter_class: Type[StrategyAdapter]):
        """注册策略适配器"""
        cls._adapters[strategy_path] = adapter_class

    @classmethod
    def get_adapter(cls, strategy_path: str) -> StrategyAdapter:
        """获取策略适配器实例"""
        if strategy_path not in cls._adapters:
            raise ValueError(
                f"未找到策略 '{strategy_path}' 的适配器。"
                f"已注册的策略: {list(cls._adapters.keys())}"
            )
        adapter_class = cls._adapters[strategy_path]
        return adapter_class()

    @classmethod
    def list_strategies(cls) -> List[str]:
        """列出所有已注册的策略"""
        return list(cls._adapters.keys())

# 注册内置适配器
from adapters.tsla_v7_2_adapter import TSLAV72Adapter
from adapters.reve_adapter import RevEAdapter

StrategyRegistry.register("strategies.tsla_v7_2.TSLASimplifiedV7_2", TSLAV72Adapter)
StrategyRegistry.register("strategies.reve_intraday_strategy.RevEIntradayStrategyV2", RevEAdapter)
```

---

#### 5. 重构后的 WalkForwardHPO

```python
# wf_hpo.py (重构后的核心部分)
from strategy_registry import StrategyRegistry

class WalkForwardHPO:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        """
        通用化的配置，不再硬编码策略
        """
        self.cfg = cfg
        self.symbols = cfg["symbols"]
        self.timeframe = cfg.get("timeframe", "1m")
        self.clickhouse = cfg.get("clickhouse", {})

        # ✅ 不再有默认值，必须显式指定策略
        strategy_path = cfg.get("strategy_path")
        if not strategy_path:
            raise ValueError(
                "配置缺少 'strategy_path'。"
                f"可用策略: {StrategyRegistry.list_strategies()}"
            )

        self.strategy_path = strategy_path
        self.strategy_base_extras = cfg.get("strategy_base_extras", {})

        # ✅ 获取策略适配器
        self.adapter = StrategyRegistry.get_adapter(strategy_path)

        # ✅ 数据由适配器加载，不再假设 dataset_df
        self.dataset_df = None  # 延迟加载

        # ... 其他初始化代码

    def _load_dataset_for_fold(self, fold: Fold) -> pd.DataFrame:
        """
        使用策略适配器加载数据
        """
        df = self.adapter.load_data(
            symbols=self.symbols,
            start=fold.is_start,
            end=fold.oos_end,  # 加载整个 fold 的数据
            clickhouse_config=self.clickhouse,
            features_table=self.cfg.get("features_table"),
            timeframe=self.timeframe
        )

        # 验证数据
        self.adapter.validate_data(df)

        return df

    def _grid_eval(
        self,
        fold: Fold,
        grid_list: List[Dict[str, Any]],
        df_slice: Optional[pd.DataFrame] = None,
    ) -> List[Dict[str, Any]]:
        """
        运行 grid search，使用适配器解析参数
        """
        # ... 前面的代码

        for idx, params in enumerate(grid_list):
            # ✅ 使用适配器解析参数
            strategy_params = self.adapter.parse_params(
                grid_params=params,
                base_extras=self.strategy_base_extras
            )

            # ... 提交回测任务
            fut = ex.submit(
                _run_engine_external,
                self._bt_cfg_dict,
                fold.is_start,
                fold.is_end,
                strategy_params,  # ✅ 使用解析后的参数
                data_for_run
            )
```

---

### 配置文件对比

#### 重构前（硬编码）

```json
{
  "hpo": {
    "strategy_path": "strategies.tsla_v7_2.TSLASimplifiedV7_2",
    "grid": {
      "rsi_period": [10, 14, 20],
      "bb_std": [1.5, 2.0, 2.5]
    }
  }
}
```

#### 重构后（通用）

```json
{
  "hpo": {
    "strategy_path": "strategies.reve_intraday_strategy.RevEIntradayStrategyV2",
    "strategy_adapter": "adapters.reve_adapter.RevEAdapter",  // ✅ 显式指定适配器
    "strategy_base_extras": {
      "universe": ["TSLA", "NVDA"],
      "risk": {
        "per_trade_pct": 0.0025
      }
    },
    "grid": {
      "strategy_A.near_vwap_atr": [0.15, 0.20, 0.25],  // ✅ 点分隔命名
      "strategy_A.sl_atr": [0.40, 0.50, 0.60]
    }
  }
}
```

---

## 🚀 第三部分：实施路线图

### Phase 1: 抽象层基础 (1-2天)

1. ✅ 创建 `strategy_adapter.py` 基类
2. ✅ 创建 `strategy_registry.py` 注册表
3. ✅ 实现 `TSLAV72Adapter` (向后兼容)
4. ✅ 添加单元测试

### Phase 2: 核心重构 (2-3天)

1. ✅ 重构 `WalkForwardHPO.__init__()` - 移除硬编码
2. ✅ 重构 `WalkForwardHPO._grid_eval()` - 使用适配器解析参数
3. ✅ 添加 `_load_dataset_for_fold()` - 策略驱动的数据加载
4. ✅ 更新 `_run_engine_external()` - 传递解析后的参数

### Phase 3: RevE 适配器 (1-2天)

1. ✅ 实现 `RevEAdapter`
2. ✅ 实现嵌套参数解析逻辑
3. ✅ 实现多数据源加载（features_1m + bars_5m）
4. ✅ 测试 RevE 策略优化

### Phase 4: 文档和示例 (1天)

1. ✅ 编写适配器开发指南
2. ✅ 提供更多策略适配器示例
3. ✅ 更新配置文件文档

---

## 📝 第四部分：适配器开发指南

### 如何为新策略创建适配器

#### Step 1: 继承 `StrategyAdapter`

```python
from strategy_adapter import StrategyAdapter

class MyStrategyAdapter(StrategyAdapter):
    pass
```

#### Step 2: 实现必需方法

```python
def load_data(self, symbols, start, end, clickhouse_config, **kwargs):
    """
    加载策略需要的数据

    Tips:
    - 从 ClickHouse 或其他数据源加载
    - 确保返回的 DataFrame 包含所有必需列
    - 可以加载多个表并合并
    """
    pass

def validate_data(self, df):
    """验证数据完整性"""
    pass

def parse_params(self, grid_params, base_extras):
    """
    解析参数结构

    Tips:
    - 处理嵌套参数
    - 解析点分隔路径
    - 合并 grid_params 和 base_extras
    """
    pass
```

#### Step 3: 注册适配器

```python
from strategy_registry import StrategyRegistry

StrategyRegistry.register(
    "strategies.my_strategy.MyStrategy",
    MyStrategyAdapter
)
```

#### Step 4: 配置文件

```json
{
  "hpo": {
    "strategy_path": "strategies.my_strategy.MyStrategy",
    "grid": {
      "param1": [1, 2, 3]
    }
  }
}
```

---

## 🎯 第五部分：收益分析

### 重构前的问题

| 问题 | 影响 |
|------|------|
| 策略路径硬编码 | 只能优化 TSLA V7_2 |
| 数据加载硬编码 | 无法支持多数据源策略 |
| 参数结构硬编码 | 无法支持嵌套参数 |
| 列名假设硬编码 | 策略必须适配框架 |
| ML 集成硬编码 | 所有策略必须支持 ML |

### 重构后的收益

| 收益 | 说明 |
|------|------|
| ✅ 策略无关 | 任何策略都可以优化 |
| ✅ 数据灵活 | 支持单表、多表、多源 |
| ✅ 参数灵活 | 支持扁平、嵌套、任意结构 |
| ✅ 可扩展 | 新策略只需实现适配器 |
| ✅ 向后兼容 | 现有策略无需修改 |
| ✅ 类型安全 | 适配器提供明确的接口 |
| ✅ 可测试 | 每个适配器独立测试 |

---

## 📊 第六部分：对比示例

### 运行 TSLA V7_2（重构后）

```bash
python alpha_loop.py \
  --config config/hpo_tsla_v7_2.json \
  --provider ml \
  --gens 3
```

**配置文件** (`config/hpo_tsla_v7_2.json`):
```json
{
  "hpo": {
    "strategy_path": "strategies.tsla_v7_2.TSLASimplifiedV7_2",
    "features_table": "trading.features_1m",
    "grid": {
      "rsi_period": [10, 14, 20]
    }
  }
}
```

✅ **无需修改代码，自动使用 TSLAV72Adapter**

---

### 运行 RevE（重构后）

```bash
python alpha_loop.py \
  --config config/hpo_reve_intraday.json \
  --provider ml \
  --gens 3
```

**配置文件** (`config/hpo_reve_intraday.json`):
```json
{
  "hpo": {
    "strategy_path": "strategies.reve_intraday_strategy.RevEIntradayStrategyV2",
    "features_table": "trading.features_1m",
    "strategy_base_extras": {
      "universe": ["TSLA", "NVDA"],
      "risk": {
        "per_trade_pct": 0.0025
      }
    },
    "grid": {
      "strategy_A.near_vwap_atr": [0.15, 0.20, 0.25],
      "strategy_A.sl_atr": [0.40, 0.50, 0.60]
    }
  }
}
```

✅ **自动使用 RevEAdapter，正确解析嵌套参数**

---

## 🔧 第七部分：迁移指南

### 现有用户（TSLA V7_2）

**无需任何修改！**

重构后的框架完全向后兼容。你的现有配置文件和代码继续工作。

### 新策略开发者

1. 实现策略类（不变）
2. **新增**: 实现策略适配器
3. **新增**: 注册适配器
4. 编写配置文件运行 HPO

---

## 📌 总结

### 核心改进

1. **解耦策略和框架**: 通过适配器模式实现插件化
2. **消除硬编码**: 所有策略特定逻辑移到适配器
3. **提升可扩展性**: 新策略只需实现接口
4. **保持简洁**: 框架代码更清晰，职责单一

### 下一步行动

1. Review 这份分析文档
2. 开始实施 Phase 1（抽象层基础）
3. 逐步迁移现有代码
4. 添加单元测试和集成测试

---

**文档版本**: v1.0
**日期**: 2025-10-26
**作者**: Trading Team
