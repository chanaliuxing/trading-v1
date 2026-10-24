# StrategyEngine 隔离升级计划 (v1.0)

**文档状态**: 草案
**创建日期**: 2025-10-17
**负责人**: Gemini

---

## 1. 背景与问题

当前 `StrategyEngine` 的设计中，所有策略实例共享同一个 `DataProvider` 和 `PortfolioService` 实例。这种设计虽然在早期简化了实现，但存在明显的缺陷：

*   **缺乏隔离性**: 任何一个策略如果对共享服务造成阻塞（如慢查询）、产生异常或修改了共享状态，都可能影响到引擎中运行的所有其他策略。
*   **资源争抢**: 多个策略并发访问共享资源，可能导致性能瓶颈和不可预期的行为。
*   **可扩展性差**: 在增加更多策略时，共享资源的压力会越来越大，使得系统难以水平扩展和维护。

此问题与系统早期设计文档 (`doc/system/23.md`, `doc/system/24.md`) 中提倡的“事件驱动”和“解耦”理念存在偏差，有必要进行修正。

## 2. 升级目标

本次升级的核心目标是实现**策略资源的完全隔离**，具体包括：

1.  **运行时隔离**: 每个策略应在独立的上下文中运行。
2.  **资源隔离**: 为每个策略实例提供独立的 `DataProvider` 和 `PortfolioService` 实例，杜绝副作用。
3.  **安全平滑**: 升级过程必须对现有系统影响最小，支持灰度发布和快速回滚。

## 3. 方案设计：渐进式升级

为降低风险，我们采用**基于功能开关（Feature Flag）的渐进式方案**，允许新旧两种模式并存，并逐步迁移。

### 3.1. 核心思路

在策略配置中引入一个布尔开关 `use_isolated_context`。`StrategyEngine` 在加载策略时会读取此开关：
*   若为 `true`，则为该策略创建一套全新的、隔离的依赖（`DataProvider`, `PortfolioService`）。
*   若为 `false`（默认值），则继续使用传统的、全局共享的依赖实例。

这使得我们可以逐个策略地迁移，而不影响系统的整体稳定性。

### 3.2. 实施步骤

1.  **增加配置开关**: 在 `config.yml` 的策略定义中增加 `use_isolated_context: false` 字段。
2.  **实现双路径逻辑**: 修改 `StrategyEngine`，根据上述开关，实现创建隔离实例或使用共享实例的双路径逻辑。
3.  **选择试点策略**: 选择一个非核心或新开发的策略，将其开关设置为 `true`，在 `shadow` 或 `paper` 环境中进行试点。
4.  **观察与验证**: 监控试点策略的日志、性能和行为，并确认其他策略不受任何影响。
5.  **逐步推广**: 在试点成功后，逐步将其余策略的开关打开，完成全部迁移。

## 4. 代码修改方案

以下是具体的代码修改示例，用于指导落地开发。

### 4.1. 配置文件 (`config.yml`)

为每个策略增加 `use_isolated_context` 开关，默认为 `false`。

```yaml
# config.yml

strategies:
  - strategy_id: MyStrategyA
    module: strategies.my_strategy_a
    symbols: [AAPL, GOOG]
    cadence_s: 5
    # 新增开关，默认为 false，保持现有行为
    use_isolated_context: false
    params:
      ...

  - strategy_id: MyPilotStrategyB
    module: strategies.my_pilot_b
    symbols: [TSLA]
    cadence_s: 10
    # 为试点策略开启隔离模式
    use_isolated_context: true
    params:
      ...
```

### 4.2. 策略引擎 (`services/strategy/strategy_engine.py`)

这是本次修改的核心。

```python
# services/strategy/strategy_engine.py (示意代码)
import asyncio
from your_services import DataProvider, PortfolioService # 假设的服务导入

class StrategyEngine:
    """
    修改后的策略引擎，支持双路径依赖注入。
    """
    def __init__(self, bus, db_connection_pool, specs):
        self.bus = bus
        self.specs = {s.strategy_id: s for s in specs}
        self.db_connection_pool = db_connection_pool # 假设引擎启动时传入了共享的DB连接池
        self.logger = ... # 假设有日志记录器

        # --- MODIFICATION START ---

        # 1. 创建全局共享的实例，供旧模式下的策略使用
        self.logger.info("Creating shared service instances for legacy strategies...")
        self.shared_data_provider = DataProvider(db_connection_pool=self.db_connection_pool)
        self.shared_portfolio_service = PortfolioService(db_connection_pool=self.db_connection_pool, event_bus=self.bus)

        # --- MODIFICATION END ---

        self._workers: Dict[str, asyncio.Task] = {}
        self._instances: Dict[str, StrategyAlgorithm] = {}
        self._ctxs: Dict[str, StrategyContext] = {}
        # ...

    async def start(self):
        self.logger.info("Starting StrategyEngine...")
        for sid, spec in self.specs.items():
            
            # --- MODIFICATION START ---

            # 2. 从策略配置中读取功能开关
            use_isolated_context = spec.config.get("use_isolated_context", False)

            data_provider_instance: DataProvider
            portfolio_service_instance: PortfolioService

            # 3. 根据开关实现双路径逻辑
            if use_isolated_context:
                self.logger.info(f"Loading strategy '{sid}' in ISOLATED mode.")
                # 为此策略创建全新的、隔离的实例
                data_provider_instance = DataProvider(db_connection_pool=self.db_connection_pool)
                portfolio_service_instance = PortfolioService(
                    db_connection_pool=self.db_connection_pool, 
                    event_bus=self.bus, 
                    strategy_id=sid # 将 strategy_id 传入，用于服务内部的逻辑隔离
                )
            else:
                self.logger.info(f"Loading strategy '{sid}' in SHARED mode.")
                # 使用预先创建的全局共享实例
                data_provider_instance = self.shared_data_provider
                portfolio_service_instance = self.shared_portfolio_service
            
            # --- MODIFICATION END ---

            # 4. 使用最终确定的实例创建上下文并加载策略 (这部分逻辑不变)
            cls = self._import_class(spec.module)
            inst: StrategyAlgorithm = cls()
            
            ctx = StrategyContext(
                engine=self,
                sid=sid,
                config=spec.config,
                # 注入正确选择的依赖实例
                data_provider=data_provider_instance,
                portfolio_service=portfolio_service_instance,
                bus=self.bus
            )
            
            inst.initialize(ctx)
            self._instances[sid] = inst
            self._ctxs[sid] = ctx
            
            self._workers[sid] = asyncio.create_task(self._worker_loop(sid))
            self.logger.info(f"Strategy '{sid}' loaded and worker started.")
        
        # ... 启动 bar 分发器等
```

### 4.3. 核心服务构造函数 (建议)

为确保 `DataProvider` 和 `PortfolioService` 在被多次实例化时能有效共享底层资源（如数据库连接），其构造函数应遵循接受外部依赖的模式。

```python
# services/data/data_provider.py (建议模式)

class DataProvider:
    def __init__(self, db_connection_pool):
        # 不要在内部创建连接池，而是接收一个共享的池
        self.db_pool = db_connection_pool

    async def get_data(self, ...):
        async with self.db_pool.acquire() as connection:
            # ... 使用连接执行查询
            pass
```

## 5. 测试计划

为保证升级质量，必须编写以下测试：

1.  **单元测试**:
    *   测试 `StrategyEngine` 在 `use_isolated_context` 为 `true` 和 `false` 时，是否正确地为 `StrategyContext` 注入了不同（或相同）的 `DataProvider` 实例。
2.  **集成测试**:
    *   设置一个场景，同时运行两个策略：一个处于隔离模式，一个处于共享模式。
    *   **隔离性验证**: 在隔离策略的 `DataProvider` 中模拟一个长时间的延迟（`asyncio.sleep`），验证共享策略的 `DataProvider` 调用不受影响。
    *   **功能验证**: 确认两个策略都能独立地接收数据、下单和更新状态。

## 6. 工作量估算

| 模块/文件 | 修改量估算 | 说明 |
| :--- | :--- | :--- |
| **配置 (`config.yml`)** | **微小** | 每个策略增加 1 行配置。 |
| **策略引擎 (`strategy_engine.py`)** | **中等** | 核心改动，约 30-50 行，实现双路径逻辑。 |
| **核心服务 (`DataProvider`, etc.)** | **微小** | 可能需要微调构造函数，约 0-10 行。 |
| **测试代码 (`tests/...`)** | **中等至较大** | **主要工作量**，新增 50-150+ 行测试代码。 |

## 7. 风险与回滚

*   **风险**: 主要风险在于 `StrategyEngine` 的逻辑修改。如果新代码有 Bug，可能只影响开启了隔离模式的策略。
*   **回滚**: 风险极低。如果出现任何问题，只需将对应策略的 `use_isolated_context` 开关改回 `false` 并重启服务，即可恢复到升级前的状态。
