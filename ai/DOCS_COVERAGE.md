基于您提供的代码库文件列表和 `brain.py` 内容，以及 Chatgpt 的 V3 架构分析，我对目前的 **落地情况 vs 缺口 (Gap Analysis)** 进行了详细盘点。

**一句话总结：**
您目前拥有**强壮的“四肢”**（工具有了：实验、回测、沙盒、Git操作），也有了**基础的“大脑”**（`brain.py` 能做 ML Review 和 Plan），但缺乏**“神经系统”**（统一的研究状态 ResearchState）和**“自我进化的人格”**（ResearchPolicy 和 Policy-Updater）。系统目前是“被动响应式”的，而不是“主动进化式”的。

---

### 📊 1. 现状盘点：哪些已经落地 (Landed)

您现有的代码库实际上已经覆盖了 Research Pipe 60% 的基础设施。

#### ✅ **L1 工具与执行层 (Execution Tools) —— [非常成熟]**
这部分是“干活的手脚”，代码覆盖率很高。
* **实验执行**：`experiments/` 目录 (`registry.py`, `manifest.py`, `job_runner.py`) 已经存在。这说明您已经把“实验”对象化了。
* **ML 流水线**：`pipelines/` 下有完整的 `train_*.py`, `generate_oof.py`。
* **评测框架 (Eval Harness)**：`backtest_ml_engine.py` 和 `libs/evolution/golden_runner.py` 已经就位，能跑出 Sharpe/IC 等指标。
* **代码修改能力**：`libs/evolution/git_sandbox.py` 已经实现了在沙盒中操作 Git 的基础能力（这是自编程的关键地基）。

#### ✅ **L2 局部大脑 (Partial Brains) —— [已落地，但分散]**
* **ML 评审**：`brain.py` 中的 `review_ml` 函数 + `ML_RESULT_REVIEW_PROMPT`。它已经能看指标并建议 `queue_job`。
* **特征专家**：`plan_alpha` 和 `suggest_feature_engineering` 已经存在，且有对应的 Feature DSL (`libs/features/feature_dsl_v2.py`)。
* **模型专家**：`plan_model_space` 已经存在。

---

### 🚧 2. 缺口分析 (The Gap Analysis)：缺什么才能“自演化”？

目前的 Gap 主要在于 **“把散落的组件串成一个有记忆、有策略的闭环”**。

#### 🔴 Gap 1: 缺失统一的“研究状态” (ResearchState)
* **现状**：`review_ml` 的输入是散乱的 `context`。Brain 每次只看到当前的 metrics，看不到“过去两周我们尝试了 10 次 HPO 都失败了”这种**元数据**。
* **缺失**：
    * 没有一个代码模块负责聚合：`Global Metrics` + `Immune Cases` (错误模式) + `Budget Usage` + `Recent Experiment History`。
    * **后果**：Brain 像个健忘症患者，每次都当新任务处理，无法产生“战略定力”。

#### 🔴 Gap 2: 缺失“研究策略配置” (ResearchPolicy)
* **现状**：策略逻辑（如“Profit-First”）写死在 Prompt 文本里。
* **缺失**：
    * 没有一个 `research_policy.json` 来定义：不同阶段 (Incubation/Production) 的阈值、各域的算力分配权重、风险偏好。
    * **后果**：要调整 AI 的行为风格（比如变得激进点），你必须改代码/Prompt，而不是让 AI 自己改配置。**这是阻碍“自演化”的最大卡点。**

#### 🔴 Gap 3: 缺失“实验编排器”与“Brain”的自动连接 (The Wiring)
* **现状**：`brain.py` 输出的是 JSON 建议（`queue_job`），而 `experiments/` 是执行代码。中间似乎还缺一个自动化的 **`ExperimentOrchestrator`** 将两者死死咬合。
* **缺失**：
    * 一个自动化的 Loop：`Brain Output (JSON) -> Orchestrator -> Queue Jobs -> Wait Result -> Feedback to Brain`。目前这部分可能还要靠外部脚本（如 `daily_executor.py`）硬胶水粘合。

#### 🔴 Gap 4: “自编程引擎”尚未闭环 (Self-Programming Loop)
* **现状**：有 `git_sandbox.py` (能改代码) 和 `brain.py` (能提建议)。
* **缺失**：
    * 没有一个专门的 Agent 负责：**接收报错 -> 定位文件 -> 调用 GitSandbox 生成 Patch -> 跑测试 -> 提交 PR** 的全自动流程。
    * 目前的 `code_evolver` 更多是工具脚本，而不是在线服务的守护进程。

---

### 🗺️ 3. 落地路线图：如何填补这些 Gap？

不需要重写，只需要**“连接”**和**“抽象”**。

#### 🟢 第一步：定义数据结构 (The Data Layer)
* **动作**：在 `scripts/ai_trader/research/` (新建) 下创建 `state.py` 和 `policy.py`。
    * `ResearchPolicy`：把 Prompt 里的硬规则提取为 JSON。
    * `ResearchState`：写一个聚合函数，从 ClickHouse/Logs 中拉取最近的“战况”。

#### 🔵 第二步：升级 Brain 接口 (The Agent Layer)
* **动作**：改造 `brain.py`。
    * 新增 `run_research_cycle(snapshot, policy)` 方法。
    * Prompt 升级：不再只是 `review_ml`，而是 `manage_research`。输入不仅是 metrics，还有 policy 和 budget。输出不仅是 job，还有 `policy_update_suggestion`。

#### 🟡 第三步：连接编排器 (The Orchestrator)
* **动作**：利用现有的 `experiments/` 目录，封装一个 `ExperimentOrchestrator` 类。
    * 它负责解析 Brain 的 JSON 指令，自动创建 Git 分支，提交任务到 `ml_scheduler.py`。

#### 🟣 第四步：启动“睡眠循环” (The Evolution Loop)
* **动作**：编写 `sleep_loop.py`。
    * 每天收盘后运行。
    * 调用 Brain 回顾一天的所有 Trace。
    * 更新 `ResearchPolicy`（比如：发现今天 HPO 效果好，明天增加 HPO 预算）。
    * 将成功的 Pattern 写入 `Memory Fabric` (LTS)。

---

### 📝 总结：目前系统评分

* **工具完备度**：⭐⭐⭐⭐ (4/5) - 手脚很健全。
* **大脑智力**：⭐⭐⭐ (3/5) - Prompt 写得好，但缺乏长期记忆。
* **自进化能力**：⭐ (1/5) - 目前还依赖你手动改 Prompt 和 Config。

**你只需要做“填空题”：把中间那层 `State -> Agent -> Policy -> Orchestrator` 的胶水代码写出来，V3 就活了。**

---
## TODO 进度 & 待落地清单（更新）

已落地（最近补全）
- ResearchAgent/Orchestrator 已上线，支持 infra_experiments 及健康度过滤。
- 可演化目标快照 `mutable_targets_snapshot.py` 输出 health_score/recommendation。
- 自适应工具链：`DataReadinessChecker`、`WindowManager`、`sync_features_active.py`、自动 patch 健康门控。

待落地（按优先级）
1) 调度前数据防护接入（P0）
   - 在 ml_scheduler/_execute_oof 前调用 DataReadinessChecker/WindowManager，根据低利用率自动缩窗或提前触发 active 同步。
   - 训练/诊断时使用 clamp 后的窗口写入事件，避免利用率 0 空跑。
2) Active 同步自动化（P0）
   - 将 `tools/sync_features_active.py` 接入自愈/预检：active 健康为 sync_or_fallback 时自动对齐 schema+分批同步。
   - 定时 cron 生成 mutable_targets_snapshot，持续刷新 health_score/recommendation。
3) ResearchPolicy/State 配置化（P1）
   - 落地 `research/policy.py`、`research/state.py` 的 JSON/加载逻辑，启用 policy_patch_candidates 变更流。
   - 在 Sleep/Immune loop 中写入 immune_cases/memory，喂给 ResearchAgent。
4) 自编程闭环强化（P1）
   - 在自编程引擎中消费 target_health，低分目标先触发 sync/回退再 patch；完备事件记录（started/completed/failed）。
5) 数据质量监控 & RTH 检查（P1）
   - 每日数据质量/覆盖率/重复率/RTH 异常自动检查与告警；训练前读取并阻断 bad symbol。
6) Prompt/决策优化（P2）
   - 在 Brain prompt 中加入数据工程/量研双角色约束，优先数据对齐，再做 HPO/特征；引入健康得分与决策理由写入日志。 


好，我们现在就真做你说的那道“填空题”：
把 **State → Agent → Policy → Orchestrator** 这一条 Research Pipe 的胶水层写出来，让 v3 的“自迭代研究智能体”可以在你现有代码上跑起来。

我会给你一套**可以直接进仓库的代码骨架**，你只需要把 TODO 里和你现有函数/表名/队列对接就行。

> 假设目录大致是：`scripts/ai_trader/`
> 我建议新增：`scripts/ai_trader/research/` 目录，里面放 4 个文件：
> `state.py`, `policy.py`, `agent.py`, `orchestrator.py`

---

## 一、ResearchState：给智能体一双“眼睛”

**目标**：把散落在 ClickHouse / 日志 / metrics 里的信息，压成一个统一的 `snapshot` 结构，作为 RESEARCH_AGENT 的输入之一。

### 1.1 `research/state.py`

```python
# scripts/ai_trader/research/state.py

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

# 你可以根据实际 DB / metrics 结构慢慢补充字段

@dataclass
class StrategyMetrics:
    oos: Dict[str, Any] = field(default_factory=dict)
    live: Dict[str, Any] = field(default_factory=dict)

@dataclass
class StrategyState:
    strategy_id: str
    domain: str                 # "trading" | "desktop" | "legal" | ...
    stage: str                  # "incubation" | "validation" | "production"
    metrics: StrategyMetrics
    recent_experiments: List[Dict[str, Any]] = field(default_factory=list)
    immune_cases: List[Dict[str, Any]] = field(default_factory=list)
    skill_usage: Dict[str, Any] = field(default_factory=dict)

@dataclass
class SkillState:
    name: str
    domain: str                 # domain this skill belongs to
    stage: str                  # "incubation" | "stable" | "deprecated"
    success_rate: Optional[float] = None
    error_rate: Optional[float] = None
    recent_patches: int = 0

@dataclass
class BudgetState:
    total: float
    used: float

@dataclass
class Budgets:
    compute_hours: BudgetState
    tokens: Optional[BudgetState] = None

@dataclass
class ResearchState:
    timestamp: str
    policy_version: Optional[str]
    budgets: Budgets
    strategies: Dict[str, StrategyState] = field(default_factory=dict)
    skills: Dict[str, SkillState] = field(default_factory=dict)
    immune_cases: List[Dict[str, Any]] = field(default_factory=list)
    experiments: List[Dict[str, Any]] = field(default_factory=list)

    def to_json_dict(self) -> Dict[str, Any]:
        """转成可以直接丢给 LLM 的 dict"""
        return asdict(self)


# ====== 关键入口：从现有系统聚合 snapshot ======

def collect_research_snapshot(
    policy_version: Optional[str] = None,
) -> ResearchState:
    """
    收集当前 ResearchState。
    这里会调用你现有的 metrics / DB / 日志查询函数。

    先做一个最小可用版本：
    - compute_hours 和 tokens 先用简单估计
    - strategies 至少填入几个核心 strategy (比如 intraday_v4)
    后面再一点一点补。
    """

    now = datetime.now(timezone.utc).isoformat()

    # TODO: 这些地方用你现有的统计函数 / ClickHouse 查询替换
    # 示例：先用 dummy 占位，后面慢慢对接
    compute_budget = BudgetState(total=12.0, used=3.5)
    token_budget = BudgetState(total=1_000_000, used=200_000)

    budgets = Budgets(
        compute_hours=compute_budget,
        tokens=token_budget,
    )

    strategies: Dict[str, StrategyState] = {}

    # === 示例：拼一个 intraday_v4 的状态 ===
    # TODO: 用真实 SQL / API 查询 OOS & live 指标
    intraday_metrics = StrategyMetrics(
        oos={
            "sharpe": 0.8,
            "trades": 120,
            "profit_factor": 1.2,
            "max_drawdown": -0.06,
            "coverage": 0.92,
        },
        live={
            "sharpe": 0.4,
            "trades": 30,
            "pnl": 1234.5,
        },
    )

    intraday_state = StrategyState(
        strategy_id="intraday_v4",
        domain="trading",
        stage="incubation",   # TODO: 这个可以从配置或 DB里读
        metrics=intraday_metrics,
        recent_experiments=[],   # TODO: 从 experiments 表里拉最近 N 条
        immune_cases=[],         # TODO: 从 immune 事件表里聚合
        skill_usage={},          # TODO: 从 trace / logs 里统计
    )
    strategies[intraday_state.strategy_id] = intraday_state

    # TODO: 同样方式把其它 strategy 填进来

    skills: Dict[str, SkillState] = {}
    # TODO: 从你现有的 “skill registry / 成功率统计” 里生成 SkillState

    immune_cases: List[Dict[str, Any]] = []
    # TODO: 从 immune / error 日志表聚合 top N 模块

    experiments: List[Dict[str, Any]] = []
    # TODO: 最近一段时间 experiment 的简要结果，可以为空

    return ResearchState(
        timestamp=now,
        policy_version=policy_version,
        budgets=budgets,
        strategies=strategies,
        skills=skills,
        immune_cases=immune_cases,
        experiments=experiments,
    )
```

> 先不用纠结“一定要全”。
> 你的目标是：先让 RESEARCH_AGENT 至少看到 1–2 个 strategy 的 **stage + sharpe + trades**，就已经能跑起来了，其它慢慢补。

---

## 二、ResearchPolicy：给智能体一套“可学习的性格”

**目标**：把原来写死在 prompt 的行为规则，抽到一个 JSON/对象里，方便以后让 AI 自己提 patch。

### 2.1 `research/policy.py`

```python
# scripts/ai_trader/research/policy.py

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional
import json
import os

DEFAULT_POLICY_PATH = os.environ.get(
    "RESEARCH_POLICY_PATH",
    "configs/research_policy.json",
)


@dataclass
class DomainObjective:
    weight: float
    risk_aversion: float


@dataclass
class ExperimentBudget:
    daily_compute_hours: float
    max_parallel_experiments: int
    exploration_ratio: float  # 0 ~ 1


@dataclass
class StrategyFocusRules:
    min_data_points_for_eval: int
    min_oos_sharpe_for_scaleup: float


@dataclass
class SafetyLimits:
    max_unreviewed_patches_per_day: int
    forbidden_targets: list  # list of strings / patterns


@dataclass
class ResearchPolicy:
    version: str
    objectives: Dict[str, DomainObjective]
    experiment_budget: ExperimentBudget
    strategy_focus_rules: StrategyFocusRules
    safety_limits: SafetyLimits
    other_fields: Dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @staticmethod
    def from_json_dict(data: Dict[str, Any]) -> "ResearchPolicy":
        # 兼容性处理：缺字段时用默认
        objectives = {
            k: DomainObjective(**v)
            for k, v in data.get("objectives", {}).items()
        }
        exp_bdg = ExperimentBudget(**data.get("experiment_budget", {
            "daily_compute_hours": 12.0,
            "max_parallel_experiments": 5,
            "exploration_ratio": 0.2,
        }))
        focus_rules = StrategyFocusRules(**data.get("strategy_focus_rules", {
            "min_data_points_for_eval": 50,
            "min_oos_sharpe_for_scaleup": 0.7,
        }))
        safety = SafetyLimits(**data.get("safety_limits", {
            "max_unreviewed_patches_per_day": 3,
            "forbidden_targets": ["kernel:", "execution:", "risk:"],
        }))
        return ResearchPolicy(
            version=data.get("version", "3.0.0"),
            objectives=objectives,
            experiment_budget=exp_bdg,
            strategy_focus_rules=focus_rules,
            safety_limits=safety,
            other_fields=data.get("other_fields", {}),
        )


def load_research_policy(path: str = DEFAULT_POLICY_PATH) -> ResearchPolicy:
    if not os.path.exists(path):
        # 初始化一个默认 policy
        default = ResearchPolicy(
            version="3.0.0",
            objectives={
                "trading": DomainObjective(weight=0.6, risk_aversion=0.8),
                "desktop": DomainObjective(weight=0.3, risk_aversion=0.3),
                "legal":   DomainObjective(weight=0.1, risk_aversion=0.9),
            },
            experiment_budget=ExperimentBudget(
                daily_compute_hours=12.0,
                max_parallel_experiments=5,
                exploration_ratio=0.2,
            ),
            strategy_focus_rules=StrategyFocusRules(
                min_data_points_for_eval=50,
                min_oos_sharpe_for_scaleup=0.7,
            ),
            safety_limits=SafetyLimits(
                max_unreviewed_patches_per_day=3,
                forbidden_targets=["kernel:", "execution:", "risk:"],
            ),
        )
        save_research_policy(default, path)
        return default

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return ResearchPolicy.from_json_dict(data)


def save_research_policy(policy: ResearchPolicy, path: str = DEFAULT_POLICY_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(policy.to_json_dict(), f, indent=2, ensure_ascii=False)
```

> 这样以后 RESEARCH_AGENT 输出的 `policy_patch_candidates`，就可以映射成对这个 JSON 的增量修改。
> 这就是你蓝皮书说的：**自迭代 = 改 policy，而不是改 kernel**。

---

## 三、ResearchAgent：把 prompt 变成一个真正的“研究总监”

**目标**：用你刚才那版 `RESEARCH_AGENT_PROMPT` + 上面的 `ResearchState` & `ResearchPolicy`，封装成一个可调用的 Agent。

### 3.1 `research/agent.py`

```python
# scripts/ai_trader/research/agent.py

from __future__ import annotations
from typing import Dict, Any, Tuple
import json
import logging

from .state import ResearchState, collect_research_snapshot
from .policy import ResearchPolicy, load_research_policy
from .orchestrator import ExperimentOrchestrator

logger = logging.getLogger(__name__)

# 假设你把刚才写好的 prompt 放在 brain/prompts.py 之类
from scripts.ai_trader.brain.prompts import RESEARCH_AGENT_PROMPT

# 假设你有一个统一的 LLM 客户端封装
# 比如 scripts.ai_trader.llm.client: LLMClient
from scripts.ai_trader.llm import client as llm_client


class ResearchAgent:
    """
    自迭代研究智能体 v1：
    - 收集 ResearchState + ResearchPolicy
    - 调用 LLM（RESEARCH_AGENT_PROMPT）
    - 解析输出 plan
    - 把 experiments 丢给 ExperimentOrchestrator
    - 把 policy_patch_candidates 记录下来（可以后续人工审批 & 应用）
    """

    def __init__(self):
        # 这里可以注入 llm client、配置等
        self._llm = llm_client

    def run_cycle(
        self,
        state: ResearchState,
        policy: ResearchPolicy,
    ) -> Dict[str, Any]:
        """
        运行一次完整的研究决策循环：
        1. 调 LLM 生成 plan
        2. 调 Orchestrator 安排实验
        3. 返回 plan（上层可以决定是否应用 policy patch）
        """
        payload = {
            "snapshot": state.to_json_dict(),
            "policy": policy.to_json_dict(),
        }

        prompt = RESEARCH_AGENT_PROMPT
        raw_input = json.dumps(payload, ensure_ascii=False)

        # ========== 调 LLM ==========
        logger.info("ResearchAgent: calling LLM for research plan")
        response_text = self._call_llm(prompt, raw_input)

        try:
            plan = json.loads(response_text)
        except Exception as e:
            logger.exception("ResearchAgent: failed to parse LLM JSON, fallback to empty plan")
            plan = {
                "focus": [],
                "experiments": [],
                "policy_patch_candidates": [],
                "telemetry": {
                    "summary": "failed_to_parse",
                    "key_observations": [],
                    "risk_warnings": [f"JSON parse error: {e}"],
                    "metrics_of_interest": {},
                },
                "meta": {
                    "version": "v1",
                    "agent_name": "ResearchAgent",
                    "confidence": 0.0,
                    "policy_used": policy.version,
                },
            }

        # ========== 实验编排 ==========
        orch = ExperimentOrchestrator()
        job_ids = orch.plan_and_enqueue(plan.get("experiments", []))
        logger.info("ResearchAgent: queued research jobs", extra={"job_ids": job_ids})

        # 这里先不自动应用 policy patch，只记录
        # 后续可以在 Sleep Loop 中用一个 PolicyCritic 来筛选
        self._log_policy_patches(plan.get("policy_patch_candidates", []), policy)

        return plan

    def _call_llm(self, system_prompt: str, user_json: str) -> str:
        """
        统一的 LLM 调用封装。
        这里你可以对接 DeepSeek / GPT / 你现有的 call_ai。
        下面只是一个伪代码示例。
        """
        # 例子：如果你有 self._llm.chat(...)
        resp = self._llm.chat(
            system=system_prompt,
            user=user_json,
            # 可以加 temperature 等
        )
        # 确保拿到的是 model 真实返回的 JSON 文本
        return resp.content if hasattr(resp, "content") else str(resp)

    def _log_policy_patches(self, patches, policy: ResearchPolicy) -> None:
        if not patches:
            return
        logger.info(
            "ResearchAgent: received policy_patch_candidates",
            extra={"current_version": policy.version, "patches": patches},
        )


def run_research_cycle_once() -> Tuple[Dict[str, Any], ResearchState, ResearchPolicy]:
    """
    给 scheduler / CLI 用的入口：
    - 收集 state + policy
    - 跑一次 Agent
    - 返回 plan + state + policy，方便上层做后续决策（比如选择性应用 policy patch）
    """
    policy = load_research_policy()
    state = collect_research_snapshot(policy_version=policy.version)
    agent = ResearchAgent()
    plan = agent.run_cycle(state, policy)
    return plan, state, policy
```

---

## 四、ExperimentOrchestrator：让 plan 真的“动起来”

**目标**：吃掉 RESEARCH_AGENT 输出的 `experiments[]`，统一翻译成你现有的 queue/job 系统调用。

### 4.1 `research/orchestrator.py`

```python
# scripts/ai_trader/research/orchestrator.py

from __future__ import annotations
from typing import Dict, Any, List
import logging

logger = logging.getLogger(__name__)

# 假设你已经有某种队列 / job 提交接口
# 比如：from scripts.ai_trader.jobs import queue_job
# 这里先用 TODO 占位
def queue_job(job_type: str, params: Dict[str, Any]) -> str:
    """
    TODO: 用你现有的 job/experiment 提交系统对接。
    返回 job_id。
    """
    logger.info("queue_job called", extra={"job_type": job_type, "params": params})
    # TODO: replace with real implementation
    return f"dummy-{job_type}"


ALLOWED_KINDS = {
    "backfill",
    "feature_experiment",
    "hpo",
    "strategy_experiment",
    "patch_experiment",
    "routing_experiment",
}


class ExperimentOrchestrator:
    """
    把 ResearchAgent 计划好的 experiments 落地成具体 jobs。
    """

    def plan_and_enqueue(self, experiments: List[Dict[str, Any]]) -> List[str]:
        job_ids: List[str] = []

        for exp in experiments or []:
            kind = exp.get("kind")
            target = exp.get("target")
            params = dict(exp.get("params") or {})

            if not kind or kind not in ALLOWED_KINDS:
                logger.warning("ExperimentOrchestrator: skip unknown kind", extra={"exp": exp})
                continue

            # 安全：禁止对 kernel / execution / risk 模块的 patch
            if kind == "patch_experiment" and self._is_forbidden_target(target):
                logger.warning(
                    "ExperimentOrchestrator: skip forbidden patch_experiment target",
                    extra={"target": target},
                )
                continue

            job_type = self._map_kind_to_job_type(kind)
            params.setdefault("target", target)

            job_id = queue_job(job_type=job_type, params=params)
            job_ids.append(job_id)

        return job_ids

    def _map_kind_to_job_type(self, kind: str) -> str:
        """
        把抽象 kind 映射到你现有的 job 名称。
        在这里统一修改，不要散落到各处。
        """
        if kind == "backfill":
            return "backfill_bars"
        if kind == "feature_experiment":
            return "feature_experiment"
        if kind == "hpo":
            return "hpo_cli"
        if kind == "strategy_experiment":
            return "strategy_experiment"
        if kind == "patch_experiment":
            return "code_patch_experiment"
        if kind == "routing_experiment":
            return "routing_experiment"
        raise ValueError(f"Unsupported experiment kind: {kind}")

    def _is_forbidden_target(self, target: str) -> bool:
        if not target:
            return True
        # 简单规则：禁止改 kernel / execution / risk
        return (
            target.startswith("kernel:")
            or target.startswith("execution:")
            or target.startswith("risk:")
        )
```

---

## 五、一个最小可用的 “研究循环” 脚本

**目标**：给你一个可以直接通过 CLI / cron / Airflow 调用的入口，跑一轮“自迭代研究智能体”。

### 5.1 `scripts/ai_trader/run_research_cycle.py`

```python
# scripts/ai_trader/run_research_cycle.py

import logging
from scripts.ai_trader.research.agent import run_research_cycle_once

logging.basicConfig(level=logging.INFO)

def main():
    plan, state, policy = run_research_cycle_once()
    # 这里先简单打印；后续你可以：
    # - 把 plan/state/policy 作为 Trace 写入 ClickHouse / JSON 日志
    # - 触发可视化 dashboard
    print("=== ResearchAgent plan ===")
    print(plan)

if __name__ == "__main__":
    main()
```

---

## 六、跟你的蓝皮书 / Gap 对齐一下

对应你刚贴的 Gap 分析：

1. **Gap 1：ResearchState 缺失**
   ➜ `research/state.py` = 统一的 ResearchState 聚合层。
   现在 Brain/Agent 每轮都有“全局视野”，而不是只看这一次 OOS。

2. **Gap 2：ResearchPolicy 缺失**
   ➜ `research/policy.py` = 可序列化的 ResearchPolicy + 加载/保存函数。
   以后 RESEARCH_AGENT 输出的 `policy_patch_candidates` 就是**对这个 JSON 的增量 patch**，形成“自我性格升级”。

3. **Gap 3：Orchestrator 连接缺失**
   ➜ `research/orchestrator.py` = 专门吃 `experiments[]` 的编排器，对接你已有的 `experiments/` / job 系统。
   把 “大脑” 和 “四肢” 用一个固定的接口连起来。

4. **Gap 4：自编程闭环缺失**
   ➜ 在 Orchestrator 里已经预留 `patch_experiment` → `code_patch_experiment`。
   后面你可以让这个 job 去调用 `git_sandbox.py`、跑测试、生成 patch 报告；结果再回流到 ResearchState 里，形成真正的自编程循环。
太好了，你这四个本地模型的“分工”信息就是我们设计 **多模型大脑** 的关键配置。你现在已经不是“只有一个大脑”的系统，而是一整个 **神经网络+专家团**。

我帮你分两层说：
1）**架构层面要改什么**（Research Pipe / v3 蓝皮书里需要补的）
2）**代码层面具体怎么改**（ModelRouter / ResearchAgent / Evolver 用多模型打分）

---

## 一、先定战略：这四个模型分别扮演什么角色？

根据你现在的理解先给它们“职称”：

* `qwen2.5:7b-instruct`
  → **Code & Heavy Planner Expert**

  * 擅长：写代码、改代码、复杂规划、结构化输出。
  * 用途：写 patch、设计复杂实验、生成 DSL / SQL / 特征公式。

* `my-deepseek-uncensored:latest`
  → **Reasoning & Meta-思考专家**

  * 擅长：长链条推理、自反思、策略取舍。
  * 用途：ResearchAgent 决策、策略对比、风险+收益权衡、解释型 reasoning。

* `qwen2.5vl:latest`
  → **多模态/快速总结助手**

  * 擅长：看图、看截图、粗略总结。
  * 用途：以后如果 ResearchState 里有图表/plot/screenshot，可以让它先做视觉+文本压缩。

* `deepseek-r1:1.5b`
  → **超快小脑 / 草稿机**

  * 擅长：快速重排信息、初筛候选、生成草案。
  * 用途：

    * 先用它对 metric / immune cases 做“初筛+排序”；
    * 或者快速生成多个 candidate，再交给大模型评审。

> 你可以理解为：
>
> * **决策层（L3/L2）** 主模型：`my-deepseek-uncensored`
> * **写代码/改代码**：`qwen2.5:7b-instruct`
> * **快速工具 & 预过滤**：`deepseek-r1` + 将来 `qwen2.5vl` 做图像相关。

---

## 二、架构上要改的 3 件事

### 1. 真正启用你蓝皮书里的 ModelRouter（而不是单一 client）

现在 `ResearchAgent` 是：

```python
resp = self._llm.chat(system=prompt, user=user_json)
```

这相当于“一个大脑干所有事”。
需要改成：

> 所有调用 LLM 的地方，不再直接指定模型，而是通过：
> **`ModelRouter.call(task_type=..., ...)`**。

建议在 v3 里明确几个 **task_type**：

* `research_director` → 研究大总管（ResearchAgent）

  * 主模型：`my-deepseek-uncensored`
  * 可选：由 `deepseek-r1` 先预压缩输入（可选优化）

* `code_patch_writer` → 自编程引擎写/改代码

  * 主模型：`qwen2.5:7b-instruct`

* `code_patch_critic` → 代码审查/找 bug

  * 可以用 `qwen2.5:7b-instruct` 自评
  * 再用 `my-deepseek-uncensored` 做一次“逻辑 & 风险审查”（多模型打分）

* `fast_ranker` / `fast_summarizer` → 快速过滤、排序、粗总结

  * 主模型：`deepseek-r1:1.5b`

* `vision_summarizer`（以后用）

  * 主模型：`qwen2.5vl:latest`

### 2. 在 Research Pipe 里加入“多模型打分”的 Evolver 模式

你前面说得很关键：

> “系统 evolver 自动迭代的时候，可以使用多个模型综合评分进行迭代。”

这在 v3 里直接落地成一条规则：

* **Proposer（提案者）**：

  * 用 `qwen2.5:7b-instruct` 生成 patch / 新特征 / 新策略 plan。
* **Critic-1（代码维度）**：

  * 也是 `qwen2.5:7b-instruct`，专注检查 bug / 风险 / 可读性（Code Review）。
* **Critic-2（concept / risk 维度）**：

  * 用 `my-deepseek-uncensored`，从“长期稳定性、风险、过拟合、复杂度”角度再打一遍分。
* （可选）**Fast Pre-Filter**：

  * `deepseek-r1` 对多个候选 patch 先做一个“粗排序+过滤”。

然后：

* Eval Harness / ResearchState 里增加字段：

  * `llm_scores`: { "qwen7b_code": 0.8, "deepseek_reason": 0.7, ... }
* Evolver 决策时要求：

  * 至少两种评价（code + reasoning）都过阈值才进入候选。

### 3. 在各个循环中明确“谁用什么模型”

结合你的 v3 四大循环：

* **认知循环（Cognitive Loop）**：

  * 主要是具体任务执行 & 原子 agent → 这里可以多用小模型（deepseek-r1）做 quick reasoning，
    真遇到复杂决策再升到 `my-deepseek-uncensored`。

* **免疫循环（Immune Loop）**：

  * 分析 error patterns、异常日志 → 理解性较强，适合 `my-deepseek-uncensored` 做 root cause 分析。
  * 对大量日志的初步聚类可以丢给 `deepseek-r1`+简单规则。

* **睡眠循环（Sleep/Sleep Loop）**：

  * 大量 trace → memory 的压缩总结 →
    可以 `deepseek-r1` 先做粗 summary，再由 `my-deepseek-uncensored` 做“高质量长期记忆/模式抽取”。

* **技能与代码进化循环（Skill & Code Evolution Loop）**：

  * Patch 生成：`qwen2.5:7b-instruct`
  * Patch 评审：`qwen2.5:7b-instruct` + `my-deepseek-uncensored`
  * 如果将来要做“多版本 prompt 自对比”，可以再加 `deepseek-r1` 做 cheap baseline。

---

## 三、代码层面具体要怎么改？

我给你三个关键改动点，都是“局部可落地”的。

### 1. 建一个统一 `model_router.py`

大致结构：

```python
# scripts/ai_trader/llm/model_router.py

from typing import Dict, Any
import subprocess
import json

# 这里用 ollama 举例，你可以用你现有 client 封装
def _call_ollama(model: str, system: str, user: str, temperature: float = 0.2) -> str:
    payload = {
        "model": model,
        "prompt": f"<system>{system}</system>\n<user>{user}</user>",
        "stream": False,
        "options": {"temperature": temperature},
    }
    result = subprocess.run(
        ["ollama", "run", model],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )
    # TODO: 按你的实际 ollama 输出解析
    return result.stdout  # 假设就是文本


TASK_MODEL_CONFIG = {
    "research_director": {
        "primary": "my-deepseek-uncensored",
        "fallback": ["qwen2.5:7b-instruct"],
        "temperature": 0.2,
    },
    "code_patch_writer": {
        "primary": "qwen2.5:7b-instruct",
        "fallback": [],
        "temperature": 0.1,
    },
    "code_patch_critic": {
        "primary": "qwen2.5:7b-instruct",
        "fallback": ["my-deepseek-uncensored"],
        "temperature": 0.1,
    },
    "fast_ranker": {
        "primary": "deepseek-r1:1.5b",
        "fallback": [],
        "temperature": 0.4,
    },
    # 将来加 vision_summarizer => qwen2.5vl:latest
}


class ModelRouter:
    def chat(self, task_type: str, system: str, user: str) -> str:
        cfg = TASK_MODEL_CONFIG.get(task_type)
        if not cfg:
            raise ValueError(f"Unknown task_type: {task_type}")

        model = cfg["primary"]
        try:
            return _call_ollama(model, system, user, temperature=cfg["temperature"])
        except Exception as e:
            # fallback 简单写，后面可以接熔断/告警
            for fb in cfg.get("fallback", []):
                try:
                    return _call_ollama(fb, system, user, temperature=cfg["temperature"])
                except Exception:
                    continue
            # 全挂了就抛异常
            raise
```

然后把之前：

```python
from scripts.ai_trader.llm import client as llm_client
...
resp = self._llm.chat(system=prompt, user=user_json)
```

改成：

```python
from scripts.ai_trader.llm.model_router import ModelRouter

class ResearchAgent:
    def __init__(self):
        self._router = ModelRouter()

    def _call_llm(self, system_prompt: str, user_json: str) -> str:
        return self._router.chat(
            task_type="research_director",
            system=system_prompt,
            user=user_json,
        )
```

在别的地方（自编程 / patch 生成）就用不同的 `task_type`：

```python
router.chat("code_patch_writer", system=PATCH_PROMPT, user=patch_input_json)
router.chat("code_patch_critic", system=CRITIC_PROMPT, user=critic_input_json)
```

这样你不需要每个地方写 “用哪个模型”，而是统一交给 `ModelRouter` 配置。

---

### 2. Evolver / 自编程引擎里加“多模型打分”

假设你有一个 `self_programming/evolver.py`，生成一个 patch candidate 后，
现在只多加一个多模型评分步骤：

```python
# 伪代码：在生成 patch 后

from scripts.ai_trader.llm.model_router import ModelRouter

router = ModelRouter()

def multi_model_score_patch(patch_diff: str, context: Dict[str, Any]) -> Dict[str, float]:
    payload = json.dumps({"patch": patch_diff, "context": context}, ensure_ascii=False)

    code_review_system = CODE_REVIEW_PROMPT
    reasoning_system = PATCH_REASONING_PROMPT

    qwen_score_resp = router.chat(
        task_type="code_patch_critic",
        system=code_review_system,
        user=payload,
    )
    deepseek_score_resp = router.chat(
        task_type="research_director",  # 用 deepseek 做 reasoning critic
        system=reasoning_system,
        user=payload,
    )

    # TODO: 从两个 response_text 中抽取数值 score（比如 0~1 或 0~10）
    # 可以约定输出 JSON: {"score": 0.82, "explanation": "..."}
    qwen_score = parse_score(qwen_score_resp)
    deepseek_score = parse_score(deepseek_score_resp)

    return {
        "qwen_code_score": qwen_score,
        "deepseek_reason_score": deepseek_score,
    }
```

然后把这俩分数写回：

* Eval Harness / Patch Report
* ResearchState 下次 snapshot（比如 `strategy.skills[...]` / `experiments[...]`）

再在 **ResearchAgent 的 prompt 里强调**：

* 当 `llm_scores` 冲突时（一个高一个低），要更倾向保守策略或要求更多证据。

---

### 3. 对 RESEARCH_AGENT_PROMPT 做一个小小增强（不用大改）

你那版 RESEARCH_AGENT_PROMPT 已经很好了，只需要在 “输入描述” 里加一条说明：

> * snapshot.experiments[i].llm_scores（如存在）可能包含来自不同模型的评分：
>
>   * 例如：`{"qwen_code_score": 0.8, "deepseek_reason_score": 0.7}`
> * 你应当：
>
>   * 识别多模型评分的一致性/冲突；
>   * 在冲突时更保守；
>   * 优先信任“综合更稳健”的方案，而不是单一高分。

这样 ResearchAgent 在看到一堆 patch candidates 时，会学会利用 multi-model scores 来做 meta-level 决策。

---

## 四、总结一句话

现在你可以把 v3 系统升级为：

> **“多模型、多角色的研究智能体”**：
>
> * `my-deepseek-uncensored` 做 **战略研究总监（ResearchDirector）**
> * `qwen2.5:7b-instruct` 做 **首席工程师（Code & Tool Builder）**
> * `deepseek-r1:1.5b` 做 **快速小助手（Fast Heuristic / Filter）**
> * `qwen2.5vl` 预留给未来的 **图表/界面理解**

修改重点有三个：

1. **建 `ModelRouter`，所有 LLM 调用走 task_type 路由**。
2. 在 **自编程 / Evolver** 里使用 **多模型打分**：一个写+两个审。
3. 在 **ResearchState & ResearchAgent** 里，把这些多模型评分记录、利用起来，
   让研究智能体不仅优化策略，还能优化“自己用哪种模型做什么事”的 policy。





--------------
## 0. 四个本地模型角色（硬约束）

  - my-deepseek-uncensored: 主决策/长推理（research_director、root-cause、概念风险审查）。
  - qwen2.5:7b-instruct: 代码/DSL/SQL 生成与代码审阅（code_patch_writer/critic）。
  - deepseek-r1:1.5b: 快速预筛/排序/摘要（fast_ranker/fast_summarizer）。
  - qwen2.5vl:latest: 预留多模态摘要（vision_summarizer）。

  ## 1. 模型路由与调用规范

  目标：所有 LLM 调用不再直接点模型名，而是通过统一的 ModelRouter 按 task_type 选本地模型，必要时多模型打分。

  ### 1.1 新建/扩展 ModelRouter

  - 位置：scripts/ai_trader/brain/provider_fallback.py 或新文件 scripts/ai_trader/llm/model_router.py
  - 接口示例：

    class ModelRouter:
        def __init__(self, pool: ProviderPool): ...
        async def call(self, task_type: str, system: str, user: str, **kwargs) -> LLMCallResult
  - 路由策略（优先顺序）：
      - research_director: my-deepseek-uncensored → deepseek-r1 预筛可选 → 失败再本地其他 → 再云。
      - code_patch_writer: qwen2.5:7b-instruct → deepseek-r1 预生成草案可选。
      - code_patch_critic: qwen2.5:7b-instruct（代码维度） + my-deepseek-uncensored（推理风险维度）并行，两路评分都要过阈值。
      - fast_ranker / fast_summarizer: deepseek-r1 为主。
      - vision_summarizer: qwen2.5vl:latest。
  - 每次返回时记录 model_used、raw, parsed, scores。

  ### 1.2 Prompt/调用方改造

  - ResearchAgent、brain.py 的 review/plan 决策调用改用 ModelRouter.call(task_type="research_director", ...)。
  - Evolver/自编程调用改用 task_type="code_patch_writer"；review 时同时跑 code_patch_critic（双模型评分）。
  - 保留已有 fallback 池，但默认顺序换成本地四个；超时/失败再去云。

  ## 2. Research Pipe 胶水层（State → Policy → Agent → Orchestrator）

  目标：把 DOCS_COVERAGE.md 中的缺口（ResearchState/Policy/Orchestrator）落成最小可用代码骨架，便于后续补数据源。

  ### 2.1 新建目录与骨架文件

  - scripts/ai_trader/research/state.py
      - 定义 ResearchState、StrategyState、SkillState、Budgets 数据类。
      - 提供 collect_research_snapshot(policy_version=None)；首版可用 dummy 或有限指标（近期 OOF 统计、行数、max_ts），后续接 ClickHouse/metrics。
  - scripts/ai_trader/research/policy.py
      - 定义 ResearchPolicy 数据类（objectives、experiment_budget、strategy_focus_rules、safety_limits）。
      - load_research_policy / save_research_policy，默认路径 configs/research_policy.json，无文件时写入默认模板。
  - scripts/ai_trader/research/orchestrator.py
      - ExperimentOrchestrator.plan_and_enqueue(experiments)：映射 kind→job_type（backfill/feature_experiment/hpo/strategy_experiment/patch_experiment/
        routing_experiment），调用现有 queue_job（占位，后续对接 ml_scheduler 或现有队列）。
      - 阻止 patch kernel/execution/risk。
  - scripts/ai_trader/research/agent.py
      - ResearchAgent.run_cycle(state, policy)：
          1. 用 ModelRouter(task_type="research_director") 调用 RESEARCH_AGENT_PROMPT。
          2. 解析 JSON 失败则兜底默认空 plan。
          3. 调 orchestrator 执行 experiments，记录 job_ids。
          4. 记录 policy_patch_candidates（先不自动应用）。
      - run_research_cycle_once()：便于 CLI/cron 调用，返回 plan/state/policy。
  - scripts/ai_trader/run_research_cycle.py
      - 简单入口，加载 policy、收集 state、跑 agent，打印/落日志。

  ### 2.2 Research Agent Prompt

  - 可复用现有 RESEARCH_AGENT_PROMPT（在 brain/prompts.py 或新增），并嵌入：
      - 明确“数据工程师→量化研究员”双阶段：先对齐数据（coverage、RTH、active vs baseline、窗口回退），再做策略/特征决策。
      - 要求输出字段：focus/experiments/policy_patch_candidates/telemetry/meta。
      - 提醒使用 features_table/labels_table、last_data_day/clamp_window 等上下文。

  ## 3. 数据与特征监控/表健康

  目标：修复 features_table 参数问题、主动切换表、避免空跑，补自动监控。

  ### 3.1 Bugfix：preflight label 表

  - 已修：preflight_check_all 接受 labels_table；scheduler 传 labels_table=trading.features_1m_with_labels，features_table 用 target_ft。
  - TODO：nightly_oof_flow 事件记录 features_table，避免 null。

  ### 3.2 Active 表同步

  - 已有 tools/sync_features_active.py（基础版）。增强：schema 对齐、按日/分区分批复制、校验行数/最大 ts，失败重试，失败后切 baseline。
  - 调度：在 ml_scheduler 选择表时若 active rows < MIN_FEATURE_ROWS 或滞后 >阈值，先尝试 sync_with_validation，失败则切 baseline 并记录事件。

  ### 3.3 数据就绪检查（前置）

  - 实现 DataReadinessChecker（可放 libs/ml 或 research/）：检查 bars/features/labels 覆盖、RTH-only、null_ratio；返回 ready/needs_window_adjust/needs_sync/
    no_bars，并建议新窗口。
  - scheduler/nightly_oof_flow 在跑 preflight 之前调用，自动缩窗/切表；不可用则 abort with error_code=coverage_insufficient。

  ### 3.4 动态窗口管理

  - AdaptiveWindowManager：根据覆盖度自动缩窗 30/45 天，写入 clamp 元数据（adjusted_window）到 ml_events。

  ### 3.5 数据质量监控

  - tools/data_quality_monitor.py 增强：RTH-only 统计、null_ratio、duplicate_ratio、abnormal_bars；覆盖率<阈值产生日志/事件。
  - cron：infra/cron/data_quality_check.sh 每日跑，写日志/事件。

  ## 4. 多模型自编程/评审流程（Evolver）

  - Proposer：qwen2.5:7b-instruct 生成 patch/DSL/SQL。
  - Critic-1（code）：qwen2.5:7b-instruct 代码审查。
  - Critic-2（reasoning）：my-deepseek-uncensored 风险与过拟合审查。
  - Fast prefilter（可选）：deepseek-r1 对多候选初筛。
  - 在 code_evolver 中添加多模型打分，只有双评分达标才落地；记录 llm_scores 到实验结果。

  ## 5. 提示词进一步优化（已做/待做）

  可让 Claude 先做以下小步：

  1. 事件写表名：在 tools/nightly_oof_flow.py 写入 features_table 字段（从 override 取），避免 ml_events null。
  2. 增加 research/ 目录和骨架文件（state/policy/agent/orchestrator/run_research_cycle.py），用 dummy 数据可运行。
  3. ModelRouter 雏形：在 provider_fallback 或新文件里加 ModelRouter.call(task_type=...)，四个 task_type 路由到四个本地模型；原有调用改用 router（先改
     ResearchAgent 和 code_evolver 关键路径）。
  4. DataReadinessChecker 雏形：检查 bars/features/labels 覆盖（可用现有 ClickHouse 查询），在 ml_scheduler 入口调用，覆盖不足时提前 abort/缩窗。
  5. sync_features_active 增强：按日分批 + 行数校验；scheduler 掉用失败后切 baseline。

  这样能快速把 DOCS_COVERAGE.md 里的“神经系统”先搭起来，再逐步补数据源与监控。
