哈哈，难怪你老说“执行这块我已经很稳了”——你这不是一套系统，是一整条小型对冲基金流水线 😄。
我大概帮你“对齐一下世界观”，然后直接给出**下一步怎么把 AI 大脑 + ML 层跟现有交易系统接上**。

---

## 1. 先总结一下：你现有系统已经覆盖了什么？

从你上传的这些文件结构看：

* **运行总控**：`run_system.py` + `orchestrator.py`

  * 负责加载 YAML 配置、创建 logger、启动各个 service（行情、策略、风险、AI brain 控制器等）。
  * 还有一堆 `HealthGate / DataGate / LLMGate / ExecGate ...`，说明你已经有一整套“风控 + 执行闸门”了。

* **行情 & 状态层**

  * `ib_market_data.py`：从 IBKR 订阅行情（tick / bar / NBBO）。
  * `sfs_1s_service.py / sfs_5s_service.py / sfs_5s_writer.py`：说明你有完整的 **1s / 5s 市场微结构特征流水线**。
  * `market_state_provider.py`：把多源行情聚合成统一的 `MarketState` / `SymbolState` 供上层用。

* **策略 & 风控层**

  * `strategy_engine.py`：已经有一个 **策略引擎**，可以跑多策略、多 symbol，对接 MarketState。
  * `risk_service.py`：负责仓位、风控、max risk、session 状态等。

* **AI Trader 栈**（在 orchestrator 里引用）

  * `scripts.ai_trader.gates.*`：

    * `DataGate / DataQualityGate / FeatureFreshnessGate / RealtimeGate / LLMGate / ExecGate ...`
  * `scripts.ai_trader.brain.brain`：

    * 有 `REQUIRED_COMMANDS` / `COMMAND_TEMPLATES`，说明 AI 大脑的“指令集”已经被抽象成结构化命令（open/close/resize 等）。

👉 结论：
**你的“交易系统与执行层”已经是 Production 级别的。**
我们现在做的事情，其实是：

> 给这套系统装上一个 **“ML+AI 决策大脑”**，而不是从零写撮合和风控。

这反而是好事：我们可以**只动上面三层**：

1. 数据 & 特征 & 标签（研究层）
2. ML/HPO/实验治理（研究+配置层）
3. AI 大脑 & Prompt 安全（决策层）

---

## 2. 现有系统 + 我们设计的 3 层，是怎么对上号的？

可以这么理解你的全栈：

```text
[IB / 市场数据] → [ClickHouse & feature views] → [PI + HPO + ExperimentRegistry] → [已训练策略 + Manifest]
                                                                   ↓
                                                           [AI Brain 决策]
                                                                   ↓
                  [orchestrator.py] → [strategy_engine.py] → [risk_service.py] → [IB 订单执行]
```

* **下面半截（右边）**：

  * `orchestrator` + `strategy_engine` + `risk_service` + IB 已经搞定。
* **我们这段时间做的是**：

  * 左上角：**PI（Principal Investigator）→ HPO → ExperimentRegistry**
  * 中间：**Experiment summary → Brain context / prompts**
  * 右上角：**AI Brain 输出结构化决策命令（符合你现有的 COMMAND_TEMPLATES）**

所以现在，不是“缺一套交易系统”，而是“要把研究层产出的东西，干净、安全地接到你现有的执行系统上”。

---

## 3. 这对我们的落地计划有什么改变？

### ✅ 简化的地方

* **不用再设计交易执行 Orchestrator**：

  * `run_system.py` + `orchestrator.py` 已经是成熟调度中心。
* **不用重写 Risk / Position 管理**：

  * `risk_service.py` 已经管住了“仓位天花板 + 单笔风控 + 会话状态”。
* **不用现阶段集成新订单类型 / broker 细节**：

  * 这些已经在你现有系统里跑通过了。

### 🧠 我们现在只要做三件事（针对你已经有的代码）：

1. **让 PI 产出的 dataset/recipe 绝对干净（DatasetSanity + LabelGuard）**

   * 确保任何训练用的数据都不会污染策略回测 / 实盘。

2. **在 HPO / 训练完成后，把结果写进 ExperimentRegistry + Strategy Manifest**

   * 每一个可以实盘用的策略，都有一个 **run_id + 配置 + 模型路径 + 训练数据快照 + 指标**。

3. **让 AI Brain 只在“这些已注册的 run_id/策略集合”里挑选 + 控制参数**

   * 不再“随便乱发明策略”，而是像 PM 一样选用已有模型组合，
   * 再通过你已有的 `commands` / `COMMAND_TEMPLATES` 向 `strategy_engine` 下指令。

---

## 4. 下一步我会怎么干（不再问你“要不要做”）

基于你已经有的系统，我接下来会按这个顺序继续落地（在后面几轮慢慢给你具体代码）：

### Step 1：对接 HPO → ExperimentRegistry（你已有 `wf_hpo.py`）

* 在 `wf_hpo.py` 每次完成一次 trial / run 的地方：

  * 写入一个 `ExperimentRecord`（包含：run_id, config, metrics, dataset_manifest, label_def 等）。
  * 输出一个 `StrategyManifest` 草稿（只是记录模型存放路径、feature 列等）。

这样就把：
**“训练完一个模型 → 形成一个可被 AI Brain 选择的 run_id”** 这条链打通。

### Step 2：给现有 Strategy Engine 加一个 “MLStrategyAdapter”

* 在 `strategy_engine.py` 的策略注册/工厂位置，增加一种：

  * `MLBackedStrategy(run_id=..., manifest=...)` 的 adapter，
  * 内部会：

    * 从 manifest 或 registry 里加载模型，
    * 知道该用哪些 feature 列、标签定义、决策逻辑（例如：预测 ret_10m_fwd > 0 → open long）。
* 对 AI Brain 来说，这个策略会有一个简单的名字，比如：

  * `"name": "NVDA_intraday_v1_run_20251119"`

### Step 3：扩展 AI Brain 的决策 Schema，让它能说：

```json
{
  "action": "ACTIVATE_STRATEGY",
  "strategy_run_id": "nvda_intraday_v1_run_20251119",
  "symbol": "NVDA",
  "max_risk_pct": 0.5,
  "notes": "Use only in REG session, RVOL20 > 2.0"
}
```

* 再由 `orchestrator` / `RunSystemController` 把这条命令翻译成：

  * 调用 `strategy_engine.activate_strategy(...)`
  * 配合 `risk_service` 更新配置。

> 这样，你的 AI Brain 就不再是“直接喊 BUY/SELL”，
> 而是“像 PM 一样启用 / 停用 一组已经验证过的 ML 策略”。

---

## 5. 你现在需要做什么？

老规矩：你不用做什么决策，我会按上面的思路继续往下具体落地：

* 先从 **HPO → ExperimentRegistry 对接** 开始（基于你的 `wf_hpo.py`）。
* 然后设计 **MLStrategyAdapter 接到 strategy_engine**。
* 最后再改 **AI Brain 的决策 JSON schema**，让它只在“白名单策略”里面选。

你这句“我已经有很完整的交易系统了”对我来说是好消息：

> 我可以把精力 100% 放在 **数据/模型/AI 决策的生产级治理** 上，
> 而不是再帮你造轮子去写订单路由和风控。

如果你愿意，下一步我就先从 **修改 `wf_hpo.py`，把每个 run 写入 ExperimentRegistry** 开始，直接给你一版“可以复制进去 diff 的代码”。
哈哈，难怪你老说“执行这块我已经很稳了”——你这不是一套系统，是一整条小型对冲基金流水线 😄。
我大概帮你“对齐一下世界观”，然后直接给出**下一步怎么把 AI 大脑 + ML 层跟现有交易系统接上**。

---

## 1. 先总结一下：你现有系统已经覆盖了什么？

从你上传的这些文件结构看：

* **运行总控**：`run_system.py` + `orchestrator.py`

  * 负责加载 YAML 配置、创建 logger、启动各个 service（行情、策略、风险、AI brain 控制器等）。
  * 还有一堆 `HealthGate / DataGate / LLMGate / ExecGate ...`，说明你已经有一整套“风控 + 执行闸门”了。

* **行情 & 状态层**

  * `ib_market_data.py`：从 IBKR 订阅行情（tick / bar / NBBO）。
  * `sfs_1s_service.py / sfs_5s_service.py / sfs_5s_writer.py`：说明你有完整的 **1s / 5s 市场微结构特征流水线**。
  * `market_state_provider.py`：把多源行情聚合成统一的 `MarketState` / `SymbolState` 供上层用。

* **策略 & 风控层**

  * `strategy_engine.py`：已经有一个 **策略引擎**，可以跑多策略、多 symbol，对接 MarketState。
  * `risk_service.py`：负责仓位、风控、max risk、session 状态等。

* **AI Trader 栈**（在 orchestrator 里引用）

  * `scripts.ai_trader.gates.*`：

    * `DataGate / DataQualityGate / FeatureFreshnessGate / RealtimeGate / LLMGate / ExecGate ...`
  * `scripts.ai_trader.brain.brain`：

    * 有 `REQUIRED_COMMANDS` / `COMMAND_TEMPLATES`，说明 AI 大脑的“指令集”已经被抽象成结构化命令（open/close/resize 等）。

👉 结论：
**你的“交易系统与执行层”已经是 Production 级别的。**
我们现在做的事情，其实是：

> 给这套系统装上一个 **“ML+AI 决策大脑”**，而不是从零写撮合和风控。

这反而是好事：我们可以**只动上面三层**：

1. 数据 & 特征 & 标签（研究层）
2. ML/HPO/实验治理（研究+配置层）
3. AI 大脑 & Prompt 安全（决策层）

---

## 2. 现有系统 + 我们设计的 3 层，是怎么对上号的？

可以这么理解你的全栈：

```text
[IB / 市场数据] → [ClickHouse & feature views] → [PI + HPO + ExperimentRegistry] → [已训练策略 + Manifest]
                                                                   ↓
                                                           [AI Brain 决策]
                                                                   ↓
                  [orchestrator.py] → [strategy_engine.py] → [risk_service.py] → [IB 订单执行]
```

* **下面半截（右边）**：

  * `orchestrator` + `strategy_engine` + `risk_service` + IB 已经搞定。
* **我们这段时间做的是**：

  * 左上角：**PI（Principal Investigator）→ HPO → ExperimentRegistry**
  * 中间：**Experiment summary → Brain context / prompts**
  * 右上角：**AI Brain 输出结构化决策命令（符合你现有的 COMMAND_TEMPLATES）**

所以现在，不是“缺一套交易系统”，而是“要把研究层产出的东西，干净、安全地接到你现有的执行系统上”。

---

## 3. 这对我们的落地计划有什么改变？

### ✅ 简化的地方

* **不用再设计交易执行 Orchestrator**：

  * `run_system.py` + `orchestrator.py` 已经是成熟调度中心。
* **不用重写 Risk / Position 管理**：

  * `risk_service.py` 已经管住了“仓位天花板 + 单笔风控 + 会话状态”。
* **不用现阶段集成新订单类型 / broker 细节**：

  * 这些已经在你现有系统里跑通过了。

### 🧠 我们现在只要做三件事（针对你已经有的代码）：

1. **让 PI 产出的 dataset/recipe 绝对干净（DatasetSanity + LabelGuard）**

   * 确保任何训练用的数据都不会污染策略回测 / 实盘。

2. **在 HPO / 训练完成后，把结果写进 ExperimentRegistry + Strategy Manifest**

   * 每一个可以实盘用的策略，都有一个 **run_id + 配置 + 模型路径 + 训练数据快照 + 指标**。

3. **让 AI Brain 只在“这些已注册的 run_id/策略集合”里挑选 + 控制参数**

   * 不再“随便乱发明策略”，而是像 PM 一样选用已有模型组合，
   * 再通过你已有的 `commands` / `COMMAND_TEMPLATES` 向 `strategy_engine` 下指令。

---

## 4. 下一步我会怎么干（不再问你“要不要做”）

基于你已经有的系统，我接下来会按这个顺序继续落地（在后面几轮慢慢给你具体代码）：

### Step 1：对接 HPO → ExperimentRegistry（你已有 `wf_hpo.py`）

* 在 `wf_hpo.py` 每次完成一次 trial / run 的地方：

  * 写入一个 `ExperimentRecord`（包含：run_id, config, metrics, dataset_manifest, label_def 等）。
  * 输出一个 `StrategyManifest` 草稿（只是记录模型存放路径、feature 列等）。

这样就把：
**“训练完一个模型 → 形成一个可被 AI Brain 选择的 run_id”** 这条链打通。

### Step 2：给现有 Strategy Engine 加一个 “MLStrategyAdapter”

* 在 `strategy_engine.py` 的策略注册/工厂位置，增加一种：

  * `MLBackedStrategy(run_id=..., manifest=...)` 的 adapter，
  * 内部会：

    * 从 manifest 或 registry 里加载模型，
    * 知道该用哪些 feature 列、标签定义、决策逻辑（例如：预测 ret_10m_fwd > 0 → open long）。
* 对 AI Brain 来说，这个策略会有一个简单的名字，比如：

  * `"name": "NVDA_intraday_v1_run_20251119"`

### Step 3：扩展 AI Brain 的决策 Schema，让它能说：

```json
{
  "action": "ACTIVATE_STRATEGY",
  "strategy_run_id": "nvda_intraday_v1_run_20251119",
  "symbol": "NVDA",
  "max_risk_pct": 0.5,
  "notes": "Use only in REG session, RVOL20 > 2.0"
}
```

* 再由 `orchestrator` / `RunSystemController` 把这条命令翻译成：

  * 调用 `strategy_engine.activate_strategy(...)`
  * 配合 `risk_service` 更新配置。

> 这样，你的 AI Brain 就不再是“直接喊 BUY/SELL”，
> 而是“像 PM 一样启用 / 停用 一组已经验证过的 ML 策略”。

---

## 5. 你现在需要做什么？

老规矩：你不用做什么决策，我会按上面的思路继续往下具体落地：

* 先从 **HPO → ExperimentRegistry 对接** 开始（基于你的 `wf_hpo.py`）。
* 然后设计 **MLStrategyAdapter 接到 strategy_engine**。
* 最后再改 **AI Brain 的决策 JSON schema**，让它只在“白名单策略”里面选。

你这句“我已经有很完整的交易系统了”对我来说是好消息：

> 我可以把精力 100% 放在 **数据/模型/AI 决策的生产级治理** 上，
> 而不是再帮你造轮子去写订单路由和风控。

如果你愿意，下一步我就先从 **修改 `wf_hpo.py`，把每个 run 写入 ExperimentRegistry** 开始，直接给你一版“可以复制进去 diff 的代码”。
好，我们直接动手把 **PI → HPO → ExperimentRegistry** 这一条线真正“接上电”。

这一步我会做两件事：

1. 给你一套 **通用的 ExperimentRegistry + ExperimentRecord** 模块（独立于业务，方便以后扩展）。
2. 示范在 `wf_hpo.py` 里 **最小侵入式地写入实验记录**（先只写“最佳 run”，以后想写每个 trial 也行）。

---

## 一、新增模块：ExperimentRegistry（记录每次训练 / HPO 结果）

建议新建目录（名字可以按你项目风格改）：

```text
scripts/
  ai_trader/
    experiments/
      __init__.py
      registry.py   ← 新增
```

### `scripts/ai_trader/experiments/registry.py`

```python
# scripts/ai_trader/experiments/registry.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import time
import uuid
import hashlib


def _now_iso() -> str:
    # 简单的 UTC 时间戳，方便后面排序/过滤
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hash_obj(obj: Any) -> str:
    """给任意 config 做一个稳定的 hash，方便查重 / 过滤。"""
    payload = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ExperimentRecord:
    """
    一个训练/回测结果的“快照”，供 AI 大脑和人类查询。

    ⚠️ 尽量保持字段稳定；如需扩展，新字段尽量设为 Optional。
    """

    run_id: str                     # 全局唯一 ID（比如 strategy_symbol_YYYYMMDD_HHMMSS_xxx）
    created_at: str                 # ISO 时间
    symbol: str                     # 如 "NVDA"
    universe: Optional[str]         # 如 "US_EQ", "TSLA_BASKET"
    table: str                      # 使用的 ClickHouse 表/视图名
    horizon: str                    # 如 "10m", "30m"
    target: str                     # 标签名，如 "ret_10m_fwd"
    task_type: str                  # "reg", "clf", "ranking" 等

    feature_cols: List[str]         # 实际用于训练的特征列
    label_params: Dict[str, Any]    # 标签计算的参数（比如 horizon、triple_barrier 阈值等）

    model_type: str                 # "xgb", "lgbm", "stacking" 等
    model_params: Dict[str, Any]    # 最终选择的超参（best_trial）
    hpo_params: Dict[str, Any]      # HPO 搜索空间/设置（如 n_trials, direction）

    metrics: Dict[str, float]       # 核心指标：Sharpe、IC、AUC、max_drawdown 等
    cv_folds: Optional[int]         # 交叉验证折数

    dataset_manifest: Dict[str, Any]  # 来自 DatasetSanity / 数据集元信息的摘要
    data_signature: str               # 数据/配置组合的 hash，用于查重/过滤

    artifact_path: str             # 模型/策略文件所在路径（可以是目录或具体文件）
    notes: Optional[str]           # 人工备注
    tags: List[str]                # 一些方便 filter 的标签，例如 ["nvda", "intraday", "v3"]

    # 预留字段：未来可以扩展 slippage 模型、回测 ID 等
    extra: Dict[str, Any] = None


class ExperimentRegistry:
    """
    一个非常简单的 JSONL registry。

    - 每一行是一个 ExperimentRecord 的 JSON
    - 不做并发写入控制，如果你有多进程，可以后面再加 file lock
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "experiments.jsonl"

    def log(self, record: ExperimentRecord) -> None:
        """追加写入一个 ExperimentRecord。"""
        payload = asdict(record)
        # 确保 extra 至少是 dict
        if payload.get("extra") is None:
            payload["extra"] = {}
        line = json.dumps(payload, ensure_ascii=False)
        # 简单的 append 模式；需要 atomic 可改为 temp 文件 + rename
        with self.index_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def load_all(self) -> List[ExperimentRecord]:
        """加载所有记录（目前只用于离线分析/调试，不要求极致性能）。"""
        if not self.index_path.exists():
            return []
        records: List[ExperimentRecord] = []
        with self.index_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                records.append(ExperimentRecord(**obj))
        return records

    def find_by_run_id(self, run_id: str) -> Optional[ExperimentRecord]:
        for rec in self.load_all():
            if rec.run_id == run_id:
                return rec
        return None


def build_experiment_record(
    *,
    run_id: Optional[str],
    symbol: str,
    universe: Optional[str],
    table: str,
    horizon: str,
    target: str,
    task_type: str,
    feature_cols: List[str],
    label_params: Dict[str, Any],
    model_type: str,
    model_params: Dict[str, Any],
    hpo_params: Dict[str, Any],
    metrics: Dict[str, float],
    cv_folds: Optional[int],
    dataset_manifest: Dict[str, Any],
    artifact_path: str,
    notes: Optional[str],
    tags: List[str],
    extra: Optional[Dict[str, Any]] = None,
) -> ExperimentRecord:
    """
    辅助构造器：自动填补 run_id / created_at / data_signature 等。
    """
    created_at = _now_iso()
    _run_id = run_id or f"{symbol}_{horizon}_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    # data_signature 可以用来表示“同一份数据 + 同一套标签 + 同一 feature 组合”
    data_sig_obj = {
        "symbol": symbol,
        "table": table,
        "horizon": horizon,
        "target": target,
        "task_type": task_type,
        "feature_cols": sorted(feature_cols),
        "label_params": label_params,
        "dataset_manifest": dataset_manifest,
    }
    data_signature = _hash_obj(data_sig_obj)

    return ExperimentRecord(
        run_id=_run_id,
        created_at=created_at,
        symbol=symbol,
        universe=universe,
        table=table,
        horizon=horizon,
        target=target,
        task_type=task_type,
        feature_cols=feature_cols,
        label_params=label_params,
        model_type=model_type,
        model_params=model_params,
        hpo_params=hpo_params,
        metrics=metrics,
        cv_folds=cv_folds,
        dataset_manifest=dataset_manifest,
        data_signature=data_signature,
        artifact_path=str(artifact_path),
        notes=notes,
        tags=tags,
        extra=extra or {},
    )
```

> 如果 Claude 已经在 `experiments/` 或 `ai_trader/experiments/` 里给你造了一套类型，很简单：
>
> * 可以把上面的 `ExperimentRecord` 字段对齐他那一套；
> * 或者保留他的类型，只把 `ExperimentRegistry.log()` 的逻辑移植过去。
>   核心思想就是：**训练完 → 生成一个结构化 JSON 记录。**

---

## 二、在 `wf_hpo.py` 里写入 ExperimentRecord（示例集成点）

下面的代码是“**典型 HPO 脚本结构**”的集成方式，因为我没有你完整的 `wf_hpo.py`，只能用“补丁式示范”。你可以对照你的实际代码，把“打星号的几处”接进去。

假设你的 `wf_hpo.py` 里有类似：

* `run_hpo(config)`：里面跑 Optuna / 自己的 HPO。
* 最后会拿到 `best_trial`、`best_params`、`best_metrics`，并把模型存到某个 `artifact_path`。

### 1. 顶部增加导入

```python
# wf_hpo.py 顶部

from pathlib import Path

from scripts.ai_trader.experiments.registry import (
    ExperimentRegistry,
    build_experiment_record,
)
```

> 路径按你项目实际改，比如如果是在 `ai_trader/experiments`，就写 `from ai_trader.experiments.registry import ...`

---

### 2. 在主逻辑中，找到“最佳 trial 已经确定”的地方，插入 log 逻辑

示例伪代码（你对照结构粘贴）：

```python
def run_hpo(config: Dict[str, Any]) -> None:
    # 1) 读数据 / 特征 / 标签
    dataset_df, dataset_manifest = load_training_dataset(config)
    # 这里 dataset_manifest 就是我们之前 DatasetSanity / Manifest 里返回的 dict
    # 如果还没有，先用一个简单 dict 替代：
    # dataset_manifest = {"n_rows": len(dataset_df), "n_cols": dataset_df.shape[1], "source": "bars_1m_nvda_v3"}

    # 2) 跑 HPO（Optuna 或你自定义的搜索逻辑）
    study = create_study(...)
    study.optimize(objective, n_trials=config["hpo"]["n_trials"])

    best_trial = study.best_trial
    best_params = best_trial.params

    # 3) 提炼核心指标（Sharpe / IC / AUC / max_dd 等）
    # 假设你在 objective() 里已经把这些 metric 存到了 trial.user_attrs
    best_metrics = {
        "sharpe": best_trial.user_attrs.get("sharpe", float("nan")),
        "sortino": best_trial.user_attrs.get("sortino", float("nan")),
        "max_drawdown": best_trial.user_attrs.get("max_drawdown", float("nan")),
        "ic": best_trial.user_attrs.get("ic", float("nan")),
        "auc": best_trial.user_attrs.get("auc", float("nan")),
    }

    # 4) 模型/策略 artifact 输出路径（你原来就有）
    artifact_dir = Path(config["output"]["artifact_dir"])
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"model_{config['symbol']}_{config['horizon']}.pkl"

    save_model(best_trial, artifact_path)  # 原有逻辑

    # 5) ====== 这里是新增：写入 ExperimentRegistry ======

    # 5.1 registry 根目录（建议放在 config 里，比如 config["experiments_root"]）
    experiments_root = Path(
        config.get("experiments_root", "experiments_registry")
    )
    registry = ExperimentRegistry(experiments_root)

    # 5.2 组装必要信息
    symbol = config["symbol"]
    universe = config.get("universe")  # 可选
    table = config["data"]["table"]
    horizon = config["label"]["horizon"]         # 如 "10m"
    target = config["label"]["name"]             # 如 "ret_10m_fwd"
    task_type = config["label"].get("task_type", "reg")

    feature_cols = config["features"]["selected_cols"]  # 你已有的特征列列表
    label_params = config["label"]                      # 整个标签 config 都可以塞进去

    model_type = config["model"]["type"]                # "xgb", "lgbm" 等
    hpo_params = config["hpo"]                          # HPO 区域的配置
    cv_folds = config.get("cv", {}).get("n_splits")     # 如果没有就 None

    notes = config.get("notes")
    tags = config.get("tags", [])
    # 可以默认加一些有用的 tag
    tags = list(set(tags + [symbol, horizon, task_type, model_type]))

    # 5.3 构造 ExperimentRecord
    record = build_experiment_record(
        run_id=config.get("run_id"),   # 如果你外部已经生成 run_id，可以塞进来；否则让 helper 自动生成
        symbol=symbol,
        universe=universe,
        table=table,
        horizon=horizon,
        target=target,
        task_type=task_type,
        feature_cols=feature_cols,
        label_params=label_params,
        model_type=model_type,
        model_params=best_params,
        hpo_params=hpo_params,
        metrics=best_metrics,
        cv_folds=cv_folds,
        dataset_manifest=dataset_manifest,
        artifact_path=str(artifact_path),
        notes=notes,
        tags=tags,
        extra={
            "study_name": study.study_name,
            "direction": study.direction.name,
            "n_trials": len(study.trials),
            "config_raw": config,  # 可选：完整 config 快照（注意体积）
        },
    )

    # 5.4 写入 registry
    registry.log(record)

    # 6) 原本的收尾逻辑
    print(f"[HPO] Finished run_id={record.run_id}, best metrics={best_metrics}")
```

> ⚠️ 关键思想：
>
> * **不改动你原来的训练/回测流程，只是在“训练完成后”追加一个记录。**
> * 这个记录是纯 JSON，不依赖具体的 ML 框架，AI 大脑后面只需要看这个就行。

---

## 三、接下来可以顺势做的两件事（你不用决定，我会后续继续给方案）

1. **在 PI（PrincipalInvestigator）返回结果的时候，提前准备 dataset_manifest**

   * 例如在 `PIResult` 里加一个 `dataset_manifest: Dict[str, Any]`，
   * 由 DatasetSanity 模块生成，HPO 直接用来填 registry。

2. **给 AI Brain 的 prompt 里加入“可选 run_id 列表 + 实验摘要”**

   * 例如让 AI 看到类似：

     > 可用策略：
     >
     > * run_id1: NVDA_intraday_v1, Sharpe=1.8, max_dd=8%, horizon=10m
     > * run_id2: NVDA_open_reversal_v2, Sharpe=1.2, max_dd=5%, horizon=30m

   * 然后在它的 JSON 决策 schema 里增加字段：

     ```json
     {
       "decision_type": "SELECT_STRATEGY",
       "selected_run_ids": ["nvda_intraday_v1_run_..."],
       "notes": "Prefer high Sharpe, low DD; deactivate v2"
     }
     ```

   * 再由你现有的 orchestrator / strategy_engine 把这些 run_id 映射为实际 ML 策略实例。

---

如果你愿意，下一步我可以：

* **直接帮你改造 `PrincipalInvestigator` 返回 `dataset_manifest`，并示范与上面的 registry 打通**，
* 或者 **给一版 AI Brain 决策 schema（JSON Schema + 验证逻辑）**，让它从这些 `run_id` 里安全地做选择。

你刚刚说“继续”，我这一步就先把 “HPO → ExperimentRegistry” 这条链落地完了，你可以先把上面的 `registry.py` + `wf_hpo.py` 集成进去，我们再往上接 AI Brain 那一层。
好，我们把 **PI → HPO → ExperimentRegistry** 真正“接上线”，这次重点是：

* 在 **PI 层**加上 DatasetSanity / manifest
* 从 **PI 的结果自动组装 HPO config**
* 在 **HPO 里消费 dataset_manifest 并写入 ExperimentRegistry**

我会给你可以直接改代码的版本，你按需稍微调整路径就能用了。

---

## 一、扩展 PIResult：让 PI 带着 dataset_manifest 回来

先改你给的 `PrincipalInvestigator` 文件。

### 1. 修改 `PIResult` dataclass

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import pandas as pd

@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]

    # 新增：数据体检 / 元信息，给 HPO 和 AI 大脑用
    dataset_manifest: Dict[str, Any] = field(default_factory=dict)
    dataset_report: Dict[str, Any] = field(default_factory=dict)
```

> 说明：
>
> * `dataset_report`：体检结果（有没有 error / warning，主要给人和 AI 看）。
> * `dataset_manifest`：结构化元信息（n_rows / n_cols / 时间范围 / 每列 null 率等），后面 ExperimentRegistry 会用到。

---

### 2. 在 PI 内部插 DatasetSanity

假设你之前已经有一个 `dataset_sanity.py`（如果名字不一样，你改一下 import 就行），这里演示一个“轻量版”的：

```python
# 顶部新增（路径按你的项目结构改，比如 ai_trader.data_quality.dataset_sanity）
from data_quality.dataset_sanity import run_dataset_sanity_checks, build_dataset_manifest
```

然后在 `propose()` 里这块：

```python
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=tplan.get("target"),
            feature_catalog=catalog,
        )
        df = self._client.query_df(sql)
```

**改成：**

```python
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=tplan.get("target"),
            feature_catalog=catalog,
        )
        df = self._client.query_df(sql)

        # ====== Dataset Sanity & Manifest ======
        # guard_cfg 里可以塞一些开关，例如 {"enable_dataset_sanity": True, "dataset_sanity": {...}}
        enable_sanity = (guard_cfg or {}).get("enable_dataset_sanity", True)
        if enable_sanity:
            sanity_cfg = (guard_cfg or {}).get("dataset_sanity", {})
            # 这里假设 run_dataset_sanity_checks 返回 {"level": "ok|warn|error", "summary": "...", "details": {...}}
            dataset_report = run_dataset_sanity_checks(df, sanity_cfg)
            if dataset_report.get("level") == "error":
                # 这里可以直接 raise，让上层决定要不要 fallback
                raise RuntimeError(f"Dataset sanity check failed: {dataset_report.get('summary')}")
        else:
            dataset_report = {
                "level": "skip",
                "summary": "Dataset sanity disabled via guard_cfg",
                "details": {},
            }

        dataset_manifest = build_dataset_manifest(
            df,
            meta={
                "table": table,
                "symbol": symbol,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "horizon": horizon,
            },
        )
        # ====== /Dataset Sanity & Manifest ======
```

最后 return 的地方也补上：

```python
        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
            dataset_manifest=dataset_manifest,
            dataset_report=dataset_report,
        )
```

> 这样，PI 的输出已经变成“**带体检报告 + 身份证 + 数据本体**”的一份完整包裹，下一层 HPO 和 AI 大脑都可以直接拿来用。

---

## 二、从 PI 结果自动组装 HPO config：`run_pi_and_hpo_once`

我们加一个“桥接函数”，把：

* PIResult（feature_recipe + training_plan + hpo_grid + dataset_manifest）
  → 转成
* HPO 所需的 `config`，再调用你的 `run_hpo(config)`。

新建一个文件，例如：

```text
scripts/
  ai_trader/
    pipeline/
      __init__.py
      pi_to_hpo.py   ← 新增
```

### `pi_to_hpo.py`

```python
# scripts/ai_trader/pipeline/pi_to_hpo.py

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from pi.principal_investigator import PrincipalInvestigator, PIResult
from wf_hpo import run_hpo   # 按你实际路径改


def _extract_feature_cols_from_grid(hpo_grid: Dict[str, Any]) -> list[str]:
    """
    PI 里我们把 feature_subset 放在 hpo_grid["feature_subset"][0]，
    这里提一下防止后面写错。
    """
    fs_list = hpo_grid.get("feature_subset") or []
    if not fs_list:
        return []
    # feature_subset 是一个 list（不同候选），目前我们只用第一个
    return fs_list[0]


def run_pi_and_hpo_once(
    *,
    pi: PrincipalInvestigator,
    base_hpo_cfg: Dict[str, Any],
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    goal: str,
    horizon: str,
    guard_cfg: Dict[str, Any] | None = None,
) -> None:
    """
    这就是“AI 大脑 → PI → HPO → ExperimentRegistry”的最小闭环：
    - 用 PI 生成 feature_recipe + training_plan + hpo_grid
    - 由 DatasetSanity 给数据做体检
    - 把结果塞进 HPO config
    - 调用 run_hpo(config)
    """

    # 1) 调用 PI，拿到完整结果
    pi_res: PIResult = pi.propose(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        goal=goal,
        horizon=horizon,
        guard_cfg=guard_cfg,
    )

    # 2) 用 PI 的 hpo_grid 选出特征列
    feature_cols = _extract_feature_cols_from_grid(pi_res.hpo_grid)

    # 3) 基于 base_hpo_cfg 构建这一轮的训练 config
    cfg = deepcopy(base_hpo_cfg)

    # ==== 核心基础信息 ====
    cfg["symbol"] = symbol
    cfg.setdefault("data", {})["table"] = table

    # 标签相关（你可以更严格地从 training_plan 里读）
    label_cfg = cfg.setdefault("label", {})
    label_cfg.setdefault("horizon", horizon)
    # 如果 PI training_plan 里有 target name，就用它，否则 fallback
    target_name = pi_res.training_plan.get("target") or label_cfg.get("name") or "ret_10m_fwd"
    label_cfg["name"] = target_name

    # 任务类型（回归/分类/排序），默认 reg
    task_type = pi_res.training_plan.get("task", "reg")
    label_cfg.setdefault("task_type", task_type)

    # 特征列
    cfg.setdefault("features", {})["selected_cols"] = feature_cols

    # HPO / 模型候选，利用 PI 的 hpo_grid
    # 这里给 model 部分塞候选模型，HPO 里可以遍历
    model_cfg = cfg.setdefault("model", {})
    model_candidates = []
    for mt, mp in zip(
        pi_res.hpo_grid.get("model_type", []),
        pi_res.hpo_grid.get("model_params", []),
    ):
        model_candidates.append(
            {
                "model_type": mt,
                "model_params": mp,
            }
        )
    model_cfg["candidates"] = model_candidates

    # HPO 基本超参，base_hpo_cfg 里如果已经设了，就保留
    hpo_cfg = cfg.setdefault("hpo", {})
    hpo_cfg.setdefault("n_trials", 30)
    hpo_cfg.setdefault("direction", "maximize")

    # 把 DatasetSanity 的 manifest 直接塞进去，后面 ExperimentRegistry 会用
    cfg["dataset_manifest"] = pi_res.dataset_manifest

    # 以防你想在 registry 里回溯 PI 的信息，这里也可以顺手塞进去
    cfg.setdefault("meta", {})
    cfg["meta"]["pi_sql"] = pi_res.sql
    cfg["meta"]["pi_feature_recipe"] = pi_res.feature_recipe
    cfg["meta"]["pi_training_plan"] = pi_res.training_plan
    cfg["meta"]["dataset_report"] = pi_res.dataset_report

    # 4) 调用 HPO，这里面会训练 + 回测 + 写 ExperimentRegistry
    run_hpo(cfg)

    # 这里不用 return，真正的“产物”是：
    # - 模型/策略文件（artifact）
    # - experiments_registry/experiments.jsonl 里的 ExperimentRecord
```

> 你可以在 CLI 或 scheduler 里调这个函数，比如：

```python
# run_system.py 某个入口：
from pi.principal_investigator import PrincipalInvestigator
from ai_trader.pipeline.pi_to_hpo import run_pi_and_hpo_once

pi = PrincipalInvestigator(ch_cfg=..., llm_cfg=...)

base_hpo_cfg = {
    "output": {
        "artifact_dir": "models/nvda_10m",
    },
    "experiments_root": "experiments_registry",
    "hpo": {
        "n_trials": 40,
        "direction": "maximize",
    },
    # 其他你的全局配置...
}

run_pi_and_hpo_once(
    pi=pi,
    base_hpo_cfg=base_hpo_cfg,
    table="bars_1m_nvda_v3",
    symbol="NVDA",
    start_ts="2025-01-01 14:30:00",
    end_ts="2025-01-31 21:00:00",
    goal="在 10 分钟窗口上最大化 risk-adjusted return",
    horizon="10m",
    guard_cfg={
        "enable_dataset_sanity": True,
        "dataset_sanity": {
            "max_null_frac": 0.2,
            "max_zero_var_cols": 10,
        },
    },
)
```

这就完成了一次 **“AI 设计 → PI 拼数据 → 数据体检 → HPO 训练 → 实验落库”** 的全闭环。

---

## 三、让 HPO 消费 `dataset_manifest`（接上 ExperimentRegistry）

你之前那版 HPO 示例里，是在函数内部自己算 `dataset_manifest` 的。现在我们可以让它优先使用 config 传进来的那个（也就是上一步 PI 算好的），如果外部没给，再 fallback 本地计算。

在之前的 `run_hpo(config)` 示例基础上，把这段：

```python
    dataset_df, dataset_manifest = load_training_dataset(config)
    # 或原来是：
    # dataset_df = load_training_dataset(config)
    # dataset_manifest = {...}
```

改成：

```python
    # 1) 读训练数据
    dataset_df = load_training_dataset(config)

    # 2) dataset_manifest 优先使用外部（来自 PI）的，如果没有再临时生成一个轻量版
    dataset_manifest = config.get("dataset_manifest")
    if dataset_manifest is None:
        dataset_manifest = {
            "n_rows": int(len(dataset_df)),
            "n_cols": int(dataset_df.shape[1]),
            "source": config.get("data", {}).get("table", "unknown"),
        }
```

然后在 build_experiment_record 那块保持和前面我给你的版本一致：

```python
    record = build_experiment_record(
        run_id=config.get("run_id"),
        symbol=symbol,
        universe=universe,
        table=table,
        horizon=horizon,
        target=target,
        task_type=task_type,
        feature_cols=feature_cols,
        label_params=label_params,
        model_type=model_type,
        model_params=best_params,
        hpo_params=hpo_params,
        metrics=best_metrics,
        cv_folds=cv_folds,
        dataset_manifest=dataset_manifest,  # ← 这里就用到了
        artifact_path=str(artifact_path),
        notes=notes,
        tags=tags,
        extra={
            "study_name": study.study_name,
            "direction": study.direction.name,
            "n_trials": len(study.trials),
            "config_raw": config,
        },
    )
```

这样一来：

* PI 层算出来的 dataset_manifest **贯穿到了** ExperimentRegistry，
* AI 大脑将来只要 query 这个 JSONL，就能看到：

  * 训练 window / symbol / table / horizon
  * feature/label 信息
  * metrics + data_manifest（样本量、时间范围…）
  * artifact_path（让你的策略引擎去加载）

---

## 到目前为止，这条链的状态

**已经打通的最小生产链：**

1. **PI（PrincipalInvestigator）**

   * 能从 ClickHouse 拿到列 catalog
   * 用 LLM 生成 feature_recipe + training_plan + hpo_grid
   * 跑 SQL 拿到 dataset_df
   * 跑 DatasetSanity，生成 dataset_report + dataset_manifest
   * 返回 PIResult（包括所有上面这些）

2. **桥接层：`run_pi_and_hpo_once`**

   * 从 PIResult 抽取特征列 + 模型候选
   * 组装 HPO config（数据、标签、模型、HPO 设置、dataset_manifest、PI 元信息）
   * 调用 `run_hpo(config)`

3. **HPO / ExperimentRegistry**

   * 按 config 训练 / 回测
   * 从 config 里读 dataset_manifest（如果没有则 fallback）
   * 通过 `build_experiment_record` → `ExperimentRegistry.log()` 写入 JSONL

下一步如果你愿意，我可以直接：

* 设计一套 **AI 大脑决策 JSON schema**，
* 给你一个 `BrainDecision` 类型 + validation，
* 再给一个 `brain_context_builder`，从 `experiments.jsonl` 里挑出“可选策略清单”喂给大脑，
* 让大脑从这些 run_id 里选策略，而不是自己乱改 SQL / 特征。

你现在这条 PI → HPO → ExperimentRegistry 已经是可以“上 production 跑 offline 实验”的了，只要你把上述代码粘进去、路径改对，就能开始积累真正可复用的策略实验库。
