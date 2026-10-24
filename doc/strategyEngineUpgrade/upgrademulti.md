好的，这是为您整理的 **StrategyEngine 轻量级隔离（“Lite 舱壁”）实施方案 v1.0** 的最终详细版本，可以直接用于归档和指导开发。

-----

## StrategyEngine 轻量级隔离（“Lite 舱壁”）实施方案 v1.0

**文档状态**: 定稿
**版本**: 1.0
**创建日期**: 2025-10-19
**负责人**: Gemini
**目标上线**: 1-2 工作日内

### 1\. 背景与目标 (MVP)

**问题**: 当前 `StrategyEngine` 中，所有策略共享同一个 asyncio 事件循环。如果某个策略的 `on_features` 调用因 Bug、外部依赖缓慢或计算量过大而执行缓慢或卡死，将会阻塞整个事件循环，导致其他所有策略的执行延迟甚至停止，严重影响系统整体的稳定性和响应性。

**MVP 目标**:
以**最小的代码改动**（\<200 行）、**最低的风险**和**最快的上线速度**，实现策略执行层面的**运行时隔离**。核心目标是：**防止单个策略的故障（超时、连续错误）影响其他策略的正常运行**。

  * **故障隔离**: 策略执行超时或连续失败时，自动熔断该策略，暂时阻止其执行，但不影响其他策略。
  * **资源限制**: 限制单个策略可并发执行的任务数，防止其耗尽引擎资源。
  * **配置驱动**: 通过 `config.yml` 中的开关 (`isolation_mode: lite`) 控制隔离行为，支持灰度上线。
  * **完全可回退**: 将开关设置为 `off` 即可恢复原有行为，风险极低。
  * **范围限制**: 本次升级**不**涉及 `DataProvider`、`PortfolioService` 或数据库/消息队列层的任何改动，只在 `StrategyEngine` 内部增加保护机制。

### 2\. 方案设计：引入“舱壁”与“熔断器”

我们将在 `StrategyEngine` 内部为每个选择启用 `lite` 隔离模式的策略，引入一套轻量级的运行时保护机制：

1.  **策略执行舱壁 (`StrategyBulkhead`)**:

      * **独立队列**: 每个 `lite` 模式的策略拥有一个独立的 `asyncio.Queue`，用于缓冲待执行的任务（如 `on_features` 调用）。这提供了背压能力，当策略处理不过来时，可以丢弃任务或产生告警，而不是让任务无限堆积。
      * **并发控制**: 使用 `asyncio.Semaphore` 限制该策略同时执行的任务数量 (`max_inflight`)，防止瞬间产生过多任务冲击系统。**默认建议设为 1**，确保策略逻辑按顺序执行，避免状态竞争。
      * **超时控制**: 对每个提交到舱壁执行的任务应用 `asyncio.wait_for`，设定一个最大执行时间 (`call_timeout_s`)。超时则取消任务并记录为失败。

2.  **熔断器 (`CircuitBreaker`)**:

      * **状态跟踪**: 记录每个 `lite` 模式策略的连续失败次数（超时或异常）。
      * **自动熔断**: 当连续失败次数达到阈值 (`cb_fail_threshold`) 时，熔断器状态变为 `OPEN`。在此状态下，新的任务提交会被直接拒绝，阻止进一步执行该故障策略。
      * **自动恢复**: 熔断 `cb_half_open_after_s` 秒后，熔断器进入 `HALF_OPEN` 状态，允许**一次**任务尝试执行。如果成功，熔断器恢复到 `CLOSED` 状态；如果再次失败，则重新进入 `OPEN` 状态并开始新的冷却计时。

### 3\. 代码实施方案

#### 3.1. **新增：熔断器 (`libs/faults/circuit_breaker.py`)**

创建新文件 `libs/faults/circuit_breaker.py`。

```python
# libs/faults/circuit_breaker.py

import time
from enum import Enum
from typing import Final

DEFAULT_FAILURE_THRESHOLD: Final[int] = 5
DEFAULT_RECOVERY_TIMEOUT: Final[int] = 30  # seconds

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class CircuitBreaker:
    """
    一个简单的基于连续失败计数的熔断器状态机。

    - CLOSED: 正常状态，允许执行。成功时重置失败计数。
    - OPEN: 熔断状态，禁止执行，直到冷却时间 (recovery_timeout) 结束。
            任何失败都会重置冷却计时器。
    - HALF_OPEN: 试探状态，在冷却时间结束后自动进入。允许一次执行，
                 成功则状态变为 CLOSED，失败则状态变回 OPEN 并重新计时。
    """
    def __init__(self,
                 failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
                 recovery_timeout: int = DEFAULT_RECOVERY_TIMEOUT):
        if failure_threshold <= 0:
            raise ValueError("Failure threshold must be positive.")
        if recovery_timeout <= 0:
            raise ValueError("Recovery timeout must be positive.")

        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = 0.0

    @property
    def state(self) -> CircuitState:
        """获取当前熔断器状态，会自动处理 OPEN 到 HALF_OPEN 的转换。"""
        if self._state == CircuitState.OPEN and self._is_recovery_time_elapsed():
            # 冷却时间已过，自动进入半开状态
            return CircuitState.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        """判断当前状态是否允许执行请求。"""
        current_state = self.state # 调用 property 获取最新状态
        return current_state in [CircuitState.CLOSED, CircuitState.HALF_OPEN]

    def record_success(self):
        """记录一次成功执行。"""
        if self._state == CircuitState.HALF_OPEN:
            # 半开状态下的成功意味着可以关闭熔断器
            self._state = CircuitState.CLOSED
        # 无论如何，成功都应该重置失败计数
        self._failure_count = 0

    def record_failure(self):
        """记录一次失败执行。"""
        self._failure_count += 1
        if self._state == CircuitState.HALF_OPEN or self._failure_count >= self.failure_threshold:
            # 半开失败或达到失败阈值，都打开熔断器
            if self._state != CircuitState.OPEN:
                 # 只有在状态从未打开或从半开转换时才记录时间
                 self._opened_at = time.monotonic()
            self._state = CircuitState.OPEN
            # 重置失败计数没有意义，因为已经是 OPEN 状态

    def _is_recovery_time_elapsed(self) -> bool:
        """检查冷却时间是否已过。"""
        return time.monotonic() - self._opened_at >= self.recovery_timeout

    def __repr__(self) -> str:
        return (f"CircuitBreaker(state={self.state.value}, failures={self._failure_count}, "
                f"threshold={self.failure_threshold}, timeout={self.recovery_timeout})")

```

#### 3.2. **新增：策略执行舱壁 (`services/strategy/bulkhead.py`)**

创建新文件 `services/strategy/bulkhead.py`。

```python
# services/strategy/bulkhead.py

import asyncio
from typing import Callable, Coroutine, Final
from libs.logger import get_logger

logger = get_logger(__name__)

# 默认值
DEFAULT_QUEUE_SIZE: Final[int] = 100
DEFAULT_MAX_INFLIGHT: Final[int] = 1 # 默认单并发，保证顺序性
DEFAULT_CALL_TIMEOUT: Final[float] = 1.5 # seconds

class StrategyBulkhead:
    """
    策略执行的“舱壁”，提供任务队列、并发信号量和调用超时控制。
    确保单个策略的执行不会阻塞事件循环或过度消耗资源。
    """
    def __init__(self,
                 strategy_id: str, *,
                 queue_size: int = DEFAULT_QUEUE_SIZE,
                 max_inflight: int = DEFAULT_MAX_INFLIGHT,
                 call_timeout: float = DEFAULT_CALL_TIMEOUT):
        if queue_size <= 0:
            raise ValueError("Queue size must be positive.")
        if max_inflight <= 0:
            raise ValueError("Max inflight must be positive.")
        if call_timeout <= 0:
            raise ValueError("Call timeout must be positive.")

        self.strategy_id = strategy_id
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.semaphore = asyncio.Semaphore(max_inflight)
        self.call_timeout = call_timeout
        self._task: asyncio.Task | None = None
        self._on_task_done: Callable[[bool], None] | None = None

    async def submit(self, coro_factory: Callable[[], Coroutine]):
        """
        异步提交一个协程工厂函数到执行队列。
        如果队列已满，则放弃提交并记录警告。

        Args:
            coro_factory: 一个无参数的函数，调用时返回要执行的协程。
                          使用工厂模式确保协程在即将执行时才创建，捕获最新状态。
        """
        try:
            self.queue.put_nowait(coro_factory)
        except asyncio.QueueFull:
            logger.warning(
                "Strategy bulkhead queue is full, dropping task.",
                strategy_id=self.strategy_id,
                queue_size=self.queue.maxsize,
                current_size=self.queue.qsize()
            )
            # 可以在这里增加指标计数

    def start(self, on_task_done: Callable[[bool], None]):
        """
        启动后台工作循环，处理队列中的任务。

        Args:
            on_task_done: 一个回调函数 `(success: bool) -> None`，
                          在每个任务完成时（无论成功、失败或超时）被调用。
                          用于通知外部（如熔断器）任务结果。
        """
        if self._task is not None and not self._task.done():
            logger.warning("Strategy bulkhead already started.", strategy_id=self.strategy_id)
            return

        self._on_task_done = on_task_done
        self._task = asyncio.create_task(self._run_forever())
        logger.info("Strategy bulkhead started.", strategy_id=self.strategy_id, queue_size=self.queue.maxsize,
                    max_inflight=self.semaphore._value, call_timeout=self.call_timeout)

    async def stop(self):
        """停止后台工作循环，取消正在运行的任务。"""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.info("Strategy bulkhead task cancelled.", strategy_id=self.strategy_id)
            except Exception as e:
                logger.error("Error during bulkhead stop.", strategy_id=self.strategy_id, error=str(e))
        self._task = None
        logger.info("Strategy bulkhead stopped.", strategy_id=self.strategy_id)

    async def _run_forever(self):
        """
        主工作循环：持续从队列获取任务，通过信号量控制并发，并应用超时。
        """
        if not self._on_task_done:
            raise RuntimeError("on_task_done callback must be set before starting.")

        while True:
            try:
                factory = await self.queue.get()
            except asyncio.CancelledError:
                logger.info("Bulkhead worker loop cancelled while waiting for queue.", strategy_id=self.strategy_id)
                break # 退出循环

            async with self.semaphore:
                # 再次检查取消状态，避免在获取信号量后被取消但仍执行
                if asyncio.current_task().cancelled():
                    logger.info("Bulkhead task cancelled after acquiring semaphore.", strategy_id=self.strategy_id)
                    self.queue.task_done() # 标记任务完成，即使未执行
                    break

                task = None # 初始化 task 变量
                success = False
                try:
                    # 创建并执行协程，应用超时
                    coro = factory()
                    task = asyncio.create_task(coro)
                    await asyncio.wait_for(task, timeout=self.call_timeout)
                    success = True # 执行到这里说明未超时且未抛异常
                except asyncio.TimeoutError:
                    success = False
                    logger.error(
                        "Strategy task execution timed out.",
                        strategy_id=self.strategy_id,
                        timeout_s=self.call_timeout
                    )
                    if task and not task.done():
                        task.cancel() # 确保取消超时任务
                except asyncio.CancelledError:
                     # 如果是在 wait_for 内部被取消
                     success = False
                     logger.warning("Strategy task execution was cancelled.", strategy_id=self.strategy_id)
                     # 不需要再调用 task.cancel() 因为它已经被取消了
                except Exception as e:
                    success = False
                    logger.error(
                        "Strategy task execution failed with an exception.",
                        strategy_id=self.strategy_id,
                        error=str(e),
                        exc_info=True # 记录完整的 traceback
                    )
                    # 异常发生时 task 可能已完成或未完成，无需手动取消
                finally:
                    # 确保在任何情况下都调用回调并标记队列任务完成
                    if self._on_task_done:
                        try:
                            self._on_task_done(success)
                        except Exception as cb_e:
                            logger.error("Error in on_task_done callback.", strategy_id=self.strategy_id, error=str(cb_e))
                    self.queue.task_done()
```

#### 3.3. **核心修改：`services/strategy/engine.py`**

在 `StrategyEngine` 中集成 `StrategyBulkhead` 和 `CircuitBreaker`。

```python
# services/strategy/engine.py (修改后的版本)

import asyncio
import importlib
import time
from typing import Dict, List, Optional, Any, Type
from dataclasses import dataclass, field
from collections import defaultdict
import copy # 需要 copy

from libs.logger import get_logger
from services.strategy.strategy_base import (
    StrategyAlgorithm,
    StrategyContext,
    StrategyParams,
    DecisionEnvelope # 假设 DecisionEnvelope 在 base 中定义
)
from libs.config import config # 导入全局配置

# --- MODIFICATION START ---
from services.strategy.bulkhead import StrategyBulkhead
from libs.faults.circuit_breaker import CircuitBreaker, CircuitState
# --- MODIFICATION END ---

logger = get_logger(__name__)


class StrategyEngine:
    """
    Strategy execution engine - 支持 Lite 隔离模式 (舱壁 + 熔断器)

    Features:
    - Load and instantiate strategies dynamically.
    - Event distribution (features) to strategies.
    - Intent submission to decision pipeline.
    - (New) Lite isolation mode for fault tolerance per strategy.
    """

    def __init__(
        self,
        decision_pipeline: Optional[Any] = None, # 保持不变
        price_cache: Optional[Any] = None,       # 保持不变
    ):
        """
        Initialize strategy engine.
        """
        self.decision_pipeline = decision_pipeline
        self.price_cache = price_cache

        # Strategy instances and contexts
        self._instances: Dict[str, StrategyAlgorithm] = {}
        self._contexts: Dict[str, StrategyContext] = {}

        # --- MODIFICATION START ---
        self._specs: Dict[str, Dict] = {} # 存储从 config.yml 加载的原始策略配置
        self._bulkheads: Dict[str, StrategyBulkhead] = {}
        self._breakers: Dict[str, CircuitBreaker] = {}
        # --- MODIFICATION END ---

        # 用于跟踪通过 polling 启动的任务，以便在 shutdown 时清理
        self._poll_tasks: List[asyncio.Task] = []

        self._running = False
        logger.info("StrategyEngine initialized")

    async def load_strategy(self, strategy_id: str, name: str, params: dict):
        """
        加载、初始化并根据配置启动策略。
        这是策略进入引擎的主要入口点，由 run_system.py 调用。

        Args:
            strategy_id: 策略的唯一标识符 (来自 config.yml)。
            name: 策略的注册名称 (可能与 strategy_id 不同)。
            params: 策略参数 (来自 config.yml，已处理过)。
        """
        # --- 查找原始配置 ---
        strategy_spec_list = config.get('strategies', [])
        strategy_spec = next((s for s in strategy_spec_list if s.get('id') == strategy_id), None)

        if not strategy_spec:
            logger.error("Could not find full strategy spec in config.yml during load.", strategy_id=strategy_id)
            return

        if not strategy_spec.get('enabled', True):
             logger.info(f"Skipping disabled strategy: {strategy_id}")
             return

        module_path = f"{strategy_spec.get('module')}.{strategy_spec.get('class')}"
        logger.info(f"Loading strategy: {strategy_id} from {module_path}")

        try:
            # --- 实例化策略 ---
            parts = module_path.rsplit('.', 1)
            if len(parts) != 2:
                raise ValueError(f"Invalid module path: {module_path}")
            module_name, class_name = parts
            module = importlib.import_module(module_name)
            strategy_class: Type[StrategyAlgorithm] = getattr(module, class_name)

            # --- 创建上下文 ---
            # 注意：这里我们仍然传递 self 作为 engine 引用，保持策略代码兼容性
            ctx = StrategyContext(
                engine=self,
                strategy_id=strategy_id,
                logger_func=lambda: get_logger(f"strategies.{strategy_id}"), # 更精细的 logger
                metrics_func=None, # 需要实现 metrics 功能
                price_cache=self.price_cache
            )

            # --- 实例化 ---
            # 使用传入的、可能已处理过的 params (例如 run_system 中添加的 capital_limit)
            instance = strategy_class(ctx, StrategyParams(**params))

            self._instances[strategy_id] = instance
            self._contexts[strategy_id] = ctx
            self._specs[strategy_id] = copy.deepcopy(strategy_spec) # 存储原始配置

            # --- 初始化策略实例 ---
            await instance.initialize()
            logger.info(f"Strategy instance initialized: {strategy_id}")

            # --- 根据隔离模式启动执行单元 ---
            mode = strategy_spec.get("isolation_mode", "off")
            limits = strategy_spec.get("limits", {})

            if mode == "lite":
                logger.info(f"Starting strategy '{strategy_id}' in LITE isolation mode.")

                cb = CircuitBreaker(
                    failure_threshold=limits.get("cb_fail_threshold", 5), # 增加默认阈值
                    recovery_timeout=limits.get("cb_half_open_after_s", 60), # 增加默认恢复时间
                )
                bh = StrategyBulkhead(
                    strategy_id=strategy_id,
                    queue_size=limits.get("queue_size", DEFAULT_QUEUE_SIZE),
                    max_inflight=limits.get("max_inflight", DEFAULT_MAX_INFLIGHT),
                    call_timeout=limits.get("call_timeout_s", DEFAULT_CALL_TIMEOUT),
                )

                self._breakers[strategy_id] = cb
                self._bulkheads[strategy_id] = bh

                # 启动 Bulkhead 的后台 worker
                bh.start(on_task_done=lambda success: self._on_task_done(strategy_id, success))

            elif mode == "off":
                logger.info(f"Starting strategy '{strategy_id}' in OFF isolation mode (legacy).")
                # 对于 off 模式，不需要启动额外的 worker，
                # on_features 会被直接调用 (见下文 on_features 方法)
            else:
                logger.warning(f"Unknown isolation_mode '{mode}' for strategy {strategy_id}. Defaulting to 'off'.")

            # --- 订阅数据 (适配现有 polling 逻辑) ---
            # 假设 subscribe_features_unified 会启动 polling 任务
            # 我们需要确保 polling 的回调函数调用的是新的 on_features 入口
            # (这部分依赖于 MarketDataFacade.poll_new_bar 的实现，
            # 假设它调用 StrategyEngine.on_features)
            # 示例调用 (具体参数需根据实际情况调整):
            # await self.subscribe_features_unified(strategy_id, ["SYMBOL"], 60)

        except Exception as e:
            logger.error(f"Failed to load or initialize strategy: {strategy_id}", error=str(e), exc_info=True)
            # 清理可能已部分创建的对象
            self._instances.pop(strategy_id, None)
            self._contexts.pop(strategy_id, None)
            self._specs.pop(strategy_id, None)
            # 如果 bulkhead/breaker 已创建，需要停止
            bh = self._bulkheads.pop(strategy_id, None)
            if bh: await bh.stop()
            self._breakers.pop(strategy_id, None)


    def _on_task_done(self, strategy_id: str, success: bool):
        """舱壁任务完成后的回调，用于更新熔断器状态，并记录日志/指标。"""
        if strategy_id in self._breakers:
            cb = self._breakers[strategy_id]
            if success:
                # 记录成功，可能会关闭熔断器
                if cb.state == CircuitState.HALF_OPEN:
                    logger.info("Circuit breaker closed after successful half-open attempt.", strategy_id=strategy_id)
                cb.record_success()
            else:
                # 记录失败，可能会打开熔断器
                old_state = cb.state
                cb.record_failure()
                if old_state != CircuitState.OPEN and cb.state == CircuitState.OPEN:
                     logger.warning("Circuit breaker opened due to failures.", strategy_id=strategy_id,
                                    threshold=cb.failure_threshold)
            # 可以在这里增加 Prometheus 指标计数 (成功/失败/熔断)

    async def on_features(self, strategy_id: str, symbol: str, features: Dict[str, Any]):
        """
        接收到新特征数据时的入口点 (由外部调用，例如 MarketDataFacade 的 poller)。
        根据策略的隔离模式分发任务。
        """
        instance = self._instances.get(strategy_id)
        if not instance:
            logger.warning("Received features for unknown or unloaded strategy.", strategy_id=strategy_id)
            return

        spec = self._specs.get(strategy_id, {})
        mode = spec.get("isolation_mode", "off")

        if mode == "lite":
            cb = self._breakers.get(strategy_id)
            bh = self._bulkheads.get(strategy_id)

            if cb and bh:
                if not cb.allow():
                    # 熔断器处于 OPEN 或 HALF_OPEN (不允许第二次尝试)
                    current_state = cb.state # 获取实际状态
                    if current_state == CircuitState.OPEN:
                        # 只在 Open 状态下打印日志，避免 Half-Open 时日志过多
                        logger.warning(
                            "Strategy execution skipped due to open circuit breaker.",
                            strategy_id=strategy_id,
                            state=current_state.value
                        )
                    # 增加指标: 熔断跳过次数
                    return # 直接返回，不执行

                # 熔断器允许执行 (CLOSED 或首次 HALF_OPEN 尝试)
                # 提交任务到舱壁队列
                await bh.submit(lambda: instance.on_features(symbol, features))

            else:
                 # 配置了 lite 模式但组件未初始化，这是一个错误状态
                 logger.error("Lite mode configured but bulkhead/breaker missing.", strategy_id=strategy_id)

        else: # mode == "off"
            # 旧模式：直接在当前事件循环中 await 执行
            try:
                # 注意：这里直接 await，如果 on_features 阻塞，会影响整个引擎
                await instance.on_features(symbol, features)
            except Exception as e:
                logger.error(
                    "Strategy execution failed in 'off' mode.",
                    strategy_id=strategy_id,
                    symbol=symbol, # 添加 symbol 信息
                    error=str(e),
                    exc_info=True
                )
                # off 模式没有熔断器，失败了就只是记录错误

    async def submit_intent(self, strategy_id: str, envelope: DecisionEnvelope) -> None:
        """
        提交交易意图到决策流水线 (保持不变)。
        这个方法是由策略实例内部调用的。
        """
        if not self.decision_pipeline:
            logger.warning(f"No decision pipeline configured, intent not submitted for {strategy_id}")
            return

        try:
            # 假设 decision_pipeline 有一个处理 envelope 的方法
            # 注意：如果 decision_pipeline 本身是阻塞的，仍会影响调用它的策略（即使是lite模式）
            await self.decision_pipeline.process_decision_envelope(envelope)

            logger.info(
                "Intent submitted to decision pipeline.",
                strategy_id=strategy_id,
                symbol=envelope.symbol,
                target_qty=envelope.target_qty,
                decision_id=envelope.decision_id
            )
            # 可以在这里增加指标：提交意图次数

        except Exception as e:
            logger.error(f"Failed to submit intent for {strategy_id}", error=str(e), exc_info=True)


    async def shutdown(self):
        """优雅地关闭引擎和所有策略及其隔离组件。"""
        if not self._running:
            return
        logger.info("Shutting down StrategyEngine...")
        self._running = False

        # 停止所有 Bulkhead worker
        for strategy_id, bh in self._bulkheads.items():
            logger.info(f"Stopping bulkhead for strategy {strategy_id}...")
            await bh.stop()

        # 取消可能由 polling 启动的任务 (如果适用)
        for task in self._poll_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._poll_tasks, return_exceptions=True) # 等待取消完成

        # 调用策略实例的 shutdown 方法 (保持不变)
        for strategy_id, instance in self._instances.items():
            try:
                await instance.on_shutdown()
                logger.info(f"Strategy shutdown completed: {strategy_id}")
            except Exception as e:
                logger.error(f"Error during strategy shutdown: {strategy_id}", error=str(e))

        self._instances.clear()
        self._contexts.clear()
        self._specs.clear()
        self._bulkheads.clear()
        self._breakers.clear()
        self._poll_tasks.clear()

        logger.info("StrategyEngine shutdown complete.")

    # --- 需要保留或适配的其他方法 ---
    # 以下方法保持不变，因为策略仍然通过 self.ctx.engine 调用它们
    async def history_features_unified(self, symbol: str, bar_type: str, limit: int) -> List[Dict[str, Any]]:
        # ... (保持原有实现) ...
        pass

    async def subscribe_features_unified(self, strategy_id: str, symbols: List[str], bar_sec: int) -> None:
        # ... (保持原有实现，确保回调调用 self.on_features) ...
        pass

    async def portfolio_view(self, strategy_id: str) -> Dict[str, Any]:
        # ... (保持原有实现) ...
        pass

    async def position_view(self, strategy_id: str, symbol: str) -> Dict[str, Any]:
        # ... (保持原有实现) ...
        pass
```

#### 3.4. **配置变更 (`config.yml`)**

在 `config.yml` 文件中，为需要启用隔离的策略添加 `isolation_mode` 和 `limits` 配置块。

```yaml
# config.yml

strategies:
  - id: "tsla_v7_2_001"
    enabled: true
    module: "strategies.tsla_v7_2"
    class: "TSLASimplifiedV7_2"
    capital_limit_usd: 50000

    # --- 启用 Lite 隔离模式 ---
    isolation_mode: lite
    limits:
      max_inflight: 1         # 核心参数：保证策略逻辑顺序执行，避免状态冲突
      call_timeout_s: 1.5     # 核心参数：单次 on_features 调用超过 1.5 秒即视为超时失败
      queue_size: 100         # 缓冲队列大小，超过则丢弃新来的 feature 事件
      cb_fail_threshold: 5    # 核心参数：连续 5 次失败（超时或异常）触发熔断
      cb_half_open_after_s: 60 # 核心参数：熔断后 60 秒尝试恢复（执行一次）
    # --- 配置结束 ---

    params:
      extras:
        # ... (原有参数)

  - id: "aapl_v7_2_001"
    enabled: true
    module: "strategies.tsla_v7_2"
    class: "TSLASimplifiedV7_2"
    capital_limit_usd: 50000
    # isolation_mode: off   # (默认值，可以不写)
    params:
      extras:
        # ... (原有参数)

  # ... 其他策略 ...
```

### 4\. 测试与验收计划

1.  **单元测试 (必要)**:

      * **`CircuitBreaker`**: 编写测试覆盖 `CLOSED` -\> `OPEN` -\> `HALF_OPEN` -\> `CLOSED` 以及 `HALF_OPEN` -\> `OPEN` 的所有状态转换路径。验证 `allow()` 方法在不同状态下的返回值。验证计时器的准确性。
      * **`StrategyBulkhead`**:
          * 测试任务成功提交和执行。
          * 测试超时：提交一个长时间 sleep 的任务，验证其被 `wait_for` 超时取消，并且 `on_task_done(False)` 被调用。
          * 测试并发限制：设置 `max_inflight=1`，同时提交多个任务，验证它们是顺序执行的。设置 `max_inflight=2`，验证最多只有两个任务同时运行。
          * 测试队列已满：设置 `queue_size=1`，连续提交两个任务，验证第二个任务被丢弃并记录警告日志。
          * 测试异常处理：提交一个会抛出异常的任务，验证异常被捕获，`on_task_done(False)` 被调用，并且 bulkhead 继续处理后续任务。
          * 测试 `stop()` 方法：验证调用 `stop()` 后，正在执行的任务被取消，队列中的任务不再执行，并且 `_run_forever` 循环退出。

2.  **集成测试 (MVP 验收清单 - 手动或自动化)**:

      * **环境**: `shadow` 或 `paper` 模式。
      * **场景**:
          * 策略 A (`tsla_v7_2_001`): 配置 `isolation_mode: lite`，`call_timeout_s: 1.0`，`cb_fail_threshold: 3`，`cb_half_open_after_s: 30`。
          * 策略 B (`aapl_v7_2_001`): 配置 `isolation_mode: off`。
      * **步骤 1 (注入延迟)**: 在 `strategies/tsla_v7_2.py` 的 `on_features` 方法开头临时加入 `await asyncio.sleep(1.5)`。
      * **步骤 2 (验证隔离与熔断)**: 启动 `run_system.py`。
          * **观察点 1 (超时日志)**: 应看到策略 A (TSLA) 的日志连续出现 "Strategy task execution timed out"。
          * **观察点 2 (熔断日志)**: 在 3 次超时后，应看到策略 A 的日志出现 "Circuit breaker opened due to failures"。随后，应看到 "Strategy execution skipped due to open circuit breaker" 日志反复出现。
          * **核心验证 (其他策略)**: 同时观察策略 B (AAPL) 的日志，其 `on_features` 执行应**完全正常**，没有出现异常延迟。监控系统事件循环延迟指标（如有）应保持稳定。
      * **步骤 3 (验证恢复)**: 等待 `cb_half_open_after_s` (30秒) 过去。
          * **观察点 3 (半开尝试)**: 应看到策略 A 不再打印 "skipped" 日志，而是再次尝试执行并超时失败（因为 sleep 还在）。熔断器应立即再次打开，并重新开始打印 "skipped" 日志。
          * **观察点 4 (移除故障)**: **移除** `await asyncio.sleep(1.5)` 代码。再次等待 30 秒。
          * **观察点 5 (成功恢复)**: 熔断器进入 `HALF_OPEN`，尝试执行一次，这次**成功**。应看到日志 "Circuit breaker closed after successful half-open attempt"。之后策略 A 恢复正常执行，不再出现超时或 skipped 日志。策略 B 始终保持正常。
      * **步骤 4 (验证回滚)**: 停止系统。将策略 A 的 `isolation_mode` 改为 `off`。**重新加入** `await asyncio.sleep(1.5)`。启动系统。
          * **预期行为**: 策略 A 的 `on_features` 阻塞事件循环。策略 B 的执行**明显变慢或停止**。这验证了 `off` 模式即为旧的不隔离行为，也反证了 `lite` 模式的隔离效果。

### 5\. 风险与回滚

  * **主要风险**: `StrategyBulkhead` 或 `CircuitBreaker` 实现中的潜在 Bug。由于改动集中且独立，风险可控。`lite` 模式下的 Bug **不会**影响 `off` 模式的策略。
  * **回滚方案**: **立即生效且无损**。
    1.  停止 `run_system.py` 服务。
    2.  修改 `config.yml`，将出问题的策略的 `isolation_mode` 改为 `off`（或删除该行）。
    3.  重新启动 `run_system.py` 服务。
        系统将恢复到升级前的行为。新增的代码 (`bulkhead.py`, `circuit_breaker.py`) 不会被调用，无需删除。

### 6\. 后续扩展 (非 MVP)

此 MVP 方案为未来更强大的隔离机制奠定了基础：

  * **精细化指标**: 在 `StrategyBulkhead` 和 `CircuitBreaker` 中加入 Prometheus 指标，监控队列长度、任务延迟、超时率、熔断状态等。
  * **动态调整**: 实现通过配置热重载或控制命令，动态调整 `limits` 参数。
  * **进程级隔离**: 对于计算密集型策略，未来可以将 `StrategyBulkhead` 改造为在独立的进程中执行任务，实现更彻底的 CPU 隔离。
  * **资源配额**: 结合进程隔离，可以实现对 CPU、内存使用的硬性限制。

-----

这个方案详细、可操作，并且充分考虑了现有代码的兼容性和上线的安全性。请按此方案进行代码开发、测试和部署。