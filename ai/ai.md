Meta-Agent 工程落地蓝皮书 v3
代号：Project Taylor 副标题：Research-Integrated Edition（结合最新自演化 / 记忆 / 自编程研究）
注意,sandbox 在实际操作中可以用 git 来实现.
0. 定位 & 边界：v3 比 v2 多了什么？
0.1 系统是什么（更新版）
一个运行在云端（或本地集群）的 “自进化 AI 操作系统”：
* 能接不同领域的任务（桌面自动化 / LegalBot / Trading / 以后更多）
* 能把任务拆解、执行、记录、反思
* 能在受控范围内：
    * 更新自己的记忆（知识和技能）
    * 生成并测试新工具 / 新代码
    * 决定下一步在哪里投入“自我改进的资源”
与 v2 的区别：v3 不仅是“能自改代码的系统”，而是明确对标“持续学习 / 自演化 Agent”的最新研究：
* 长期记忆稳定
* 可持续更新
* 自编程能力
* 但始终在安全边界内运行。

新增落地模块（本仓库）：
- Critic: `libs/critic/critic_client.py` + `config/critic_policy.yaml`，在 brain plan 前读取历史统计与 heuristics。
- Heuristics表：`sql/create_heuristics.sql` + writer `scripts/ai_trader/memory/consolidate_knowledge.py`。
- Skill Registry: `config/skill_registry.json` + guidelines `docs/skills_registry.md`（含 sandbox skill `dummy_math`）。
- Evolution guardrails: `libs/evolution/git_sandbox.py`, `libs/evolution/golden_runner.py`, evolver skeleton `scripts/ai_trader/evolvers/code_evolver_git.py`, experiment DDL `sql/create_evolution_experiments.sql`.
0.2 v3 的新增目标 ✅
在 v2 的三大目标上，v3 增加 3 条“研究级”要求：
1. 可持续记忆（Continual Memory）
    * 系统可以长期运行，不靠“把全部历史塞进 prompt”。
    * 具备：记忆分层（短/中/长期）、巩固（consolidation）、遗忘（safe forgetting）。
2. 可验证的自编程能力
    * 不只是“LLM 写点 patch”，而是：
        * 能自动生成代码候选
        * 自动跑测试 / 静态分析 / 回归
        * 自动评估收益 / 风险
        * 决定是否进入候选列表
    * 和最新的“Self-Programming AI / Code-Learning Agent”方向对齐。
3. Research-Friendly：带实验接口的工程系统
    * 所有核心循环（认知 / 免疫 / 睡眠 / 代码进化）都有清晰的：
        * 输入 / 输出
        * 指标
        * 开关（可以单独启用 / 禁用）
    * 方便你：
        * 替换某一环节为新算法
        * 做 A/B 测试，和论文里的方法对比
0.3 非目标（v3 重申 + 补充） ❌
1. 不追求“彻底无人类监督”的完全自治系统。
2. 不允许 AI 改动：
    * 安全内核（权限 / 限流 / 回滚 / 风控 / 宪法）
    * 核心数据协议（Memory / Trace / Patch 的 schema）
3. 不追求一次性解决所有研究问题：
    * 异常复杂的多智能体博弈
    * 完全自动的架构重构
    * 全域策略的统一最优

1. 总体架构：四层控制 + 三环境 + 安全内核
与 v2 相比，v3 把“研究 / 自进化能力”提升为单独一层。
1.1 四层智能结构（Logic Layers v3）

L3  Research & Evolution Layer（研究与演化策略层）
 ├─ Experiment Orchestrator       # 实验编排 / 实验队列
 ├─ Evolution Strategy Manager    # 选择改哪里、怎么改
 ├─ Budget & Cost Controller      # 控制算力 / Token / 资金消耗
 └─ Policy & Governance           # 宪法级规则、合规策略

L2  Meta Layer（评估 & 自改层）
 ├─ Log & Metrics Store           # 日志 & 指标
 ├─ Eval Harness                  # 离线 & 在线评测、A/B 测试
 ├─ Self-Improvement Manager      # 驱动自我改进的总调度
 └─ Multi-Agent Coordinator       # 多智能体协作（规划 / 风控 / 成本等多视角）

L1  Base Layer（执行层）
 ├─ Orchestrator / Task Router    # 接任务 / 拆任务 / 调度模块
 ├─ Planner (Hierarchical)        # 长短期规划
 ├─ Executor                      # 调用工具 / 代码执行
 ├─ Memory Fabric                 # 记忆分层系统（短/中/长期）
 ├─ Tool & Skill Manager          # 工具 & 技能生命周期
 ├─ Critic & Self-Correction      # 评分 / 反思 / 重试
 └─ Model Router                  # gpt / deepseek / llama / 本地模型选路

L0  Infra & Kernel Layer（基础设施层）
 ├─ Safety Kernel                 # 权限、风控、回滚、审计
 ├─ Storage                       # DB / 向量库 / 对象存储
 └─ Runtime                       # Docker / K8s / Serverless / 本地进程
新增点：
* Memory System → Memory Fabric：变成“多层次记忆系统”，对齐最新 continual learning / nested memory 研究。
* 增加 L3 Research Layer：把“自我进化逻辑”拔出来，避免和正常业务混在一起。
1.2 三环境（Execution Environments）——保持，但与 Research 紧耦合
与 v2 相同，但 v3 明确：
* 所有自进化实验都必须从 Sandbox 开始。
* Research Layer 不直接操作 Production，只能发起：
    * “创建实验”
    * “提交 patch”
    * “发起 Shadow / Canary 请求”

2. 数据与记忆：从 Memory System → Memory Fabric
v2 的 Memory Schema 已经很好。v3 主要是加入“层级 + 频率 + 迁移规则”：
2.1 Memory Fabric：三层记忆体系

Short-Term Store   (STS)   # 会话级 / 任务级上下文（分钟 / 小时）
Mid-Term Store     (MTS)   # 项目级 / 版本级记忆（天 / 周）
Long-Term Store    (LTS)   # 跨项目 / 跨版本稳定知识（周 / 月 / 年）
每个 Memory 条目（v2 的 schema）多两个关键字段：

"memory_tier": "STS | MTS | LTS",
"update_policy": {
  "promotion_rule": "...",  // 什么时候从 STS 升到 MTS/LTS
  "decay_rule": "...",      // 什么时候衰减 / 冷冻 / 删除
  "consolidation_batch": "nightly | weekly | manual"
}
核心设计：
* Episodic 先进入 STS。
* 被 Sleep Loop 多次引用 / 证明有用 → 迁移到 MTS。
* 在多个 task / domain 中复用、表现稳定 → 升到 LTS（长期知识）。
* STS 定期清理；MTS 可以压缩；LTS 很少删除（只通过 supersedes 替换）。
2.2 记忆与技能的“自演化边界”
* Memory 内容（content / vector / meta / relations）可以由系统自动创建和更新。
* Memory schema（字段结构）由人维护，不允许在运行时自改。
* Skill 的 performance_stats 由系统更新；test_cases 与 safety_level 必须由人类创建 / 审核。

3. 学习与演化循环：v3 的四个闭环
v2 的三大循环（认知 / 免疫 / 睡眠）保留，v3 加一个 “技能与代码进化循环”，并且明确输入 / 输出 / 指标。
3.1 认知循环（Cognitive Loop）——实时执行闭环
与 v2 一致，这里只强调 3 点：
1. 所有步骤必须写 Trace（无 Trace 不运行）。
2. 每次任务结束后，都要生成：
    * 一份结构化 execution_summary
    * 可选 lessons_learned（由 Critic 生成）
3. 有价值的结果写入 Memory Fabric（通常到 STS/MTS）。
3.2 免疫循环（Immune Loop）——异常检测与止损
在 v2 的基础上，v3 补充：
* 增加 Anomaly Detector：
    * 使用简单统计 / ML 模型，监控 error rate / latency / cost / reward。
* 当检测到异常：
    * 自动降级：
        * 关掉部分自改功能
        * 降低探索强度
        * 切回上一稳定版本
    * 把异常期间的 Trace 打包成一个“Immune Case”，交给 Research Layer 做进一步分析。
3.3 睡眠循环（Sleep / Consolidation Loop）——记忆压缩与升级
v3 明确 Sleep Loop 的三类输出：
1. 新的 Semantic Memory（知识总结）。
2. 新的 Procedural Memory / Skills（可执行的步骤）。
3. 用于训练 / 对齐的 数据对（失败→成功、坏计划→好计划）。
同时指定：
* Sleep Loop 运行周期（每天 / 每周）
* 每次运行的资源预算（最多处理多少 Trace、最多花多少 Token）
3.4 技能与代码进化循环（Skill & Code Evolution Loop）
这是 v3 新强调的一环，从 v2 的 5.2/5.3 抽出来单独成环：
* 输入：
    * 高成功率的 Trace
    * 免疫循环标记的“表现差的模块”
* 步骤：
    1. 从 Trace 中挖掘可抽象的技能模式（pattern mining）。
    2. 由 LLM 生成对应技能代码 + 测试（Skill Candidate）。
    3. 在 Sandbox 跑测试 + 回归。
    4. 通过 Eval Harness 和老技能比较。
    5. 决定：
        * 替换旧技能 / 并存 / 丢弃。
* 输出：
    * 新技能定义（Skill Schema）
    * 对旧技能的 supersedes 记录
    * 对 Patch 的评估报告

4. 自编程引擎（Self-Programming Engine）
v3 把“自改代码”设计为一个独立子系统，而不是附属于某个域。
4.1 自编程引擎的职责
1. 接收“需要改进的模块列表”（来自 Meta Layer / 免疫循环）。
2. 为每个模块生成：
    * 若干 Patch 候选（代码 diff）
    * 对应测试（如果缺测试，先补测试）
3. 在 Sandbox + Staging 环境跑完：
    * 单元测试
    * 回归测试
    * 性能测试
    * 静态分析 / Fuzzing
4. 生成结构化的 Patch Report：
    * 覆盖率
    * 指标变化（成功率 / 延迟 / 成本等）
    * 风险评估
5. 交给：
    * Research Layer 做决策
    * 或某些低风险域（Desktop）自动提交 candidate 给 Canary。
4.2 自编程的边界
* 只允许操作 /mutable/ 下被标记为 “auto_updatable”的模块。
* 不允许修改：
    * /kernel/
    * 交易 Execution / 风控模块
    * Schema 定义模块
* 对 Legal / Trading 的业务逻辑：
    * 只能改工具 / 辅助逻辑
    * 核心推理 / 风控仍需人工 Gate 和评审。

5. 多智能体与模型路由：从“结构”到“策略”
v2 已有 Multi-Agent Coordinator 和 ModelRouter。v3 强调两个点：
5.1 多智能体的“角色固定 + 可实验性”
* 固定基础角色：
    * Speed-Agent（优先快）
    * Safety-Agent（优先安全）
    * Cost-Agent（优先便宜）
    * Domain-Expert-Agent（领域知识优先）
* 允许在 Research Layer：
    * 引入新的 Agent 角色，做小流量实验。
    * 比较不同组合下的效果（比如：安全 + 成本 vs 安全 + 收益）。
5.2 模型路由的“数据驱动”
ModelRouter 不再人工写死，而是：
* 对不同任务类型记录：
    * 使用哪个模型
    * 达成效果（成功率/满意度/收益）
    * 成本（Token/时间）
* 定期由 Sleep Loop / Research Layer：
    * 用监督学习 / bandit / RL 等方法优化路由策略。
* 但在 Trading / Legal 等高风险域：
    * 仍然由人工设定模型白名单和上限。

6. 多领域接入：v3 的新要求
三大现有域（Desktop / Legal / Trading）仍按 v2 设计。v3 额外加一个概念：
Cross-Domain Knowledge & Skill Reuse
* Memory Fabric 的 domain 字段允许 "cross" 类型：
    * 表示“可跨域共享”的知识 / 模式。
    * 例如：
        * 重试策略
        * 错误诊断模式
        * 日志分析方法
* Skill Manager 可以将某些技能重用到多个域：
    * 例如：
        * 通用的日志分析工具
        * 通用的 prompt 优化工具
        * 通用的回归测试流水线

7. 可观测性与研究接口：从“监控”提升到“实验平台”
v2 里已经有日志 / 指标 / Trace 和 Eval Harness。v3 做两件事：
7.1 把系统当成“实验框架”来设计
为 Research Layer 提供 API：
* create_experiment(config)
* run_experiment(experiment_id)
* compare_variants(variant_a, variant_b)
* export_results(experiment_id)
这样你可以：
* 把论文中的某个算法 / 策略作为一个“Variant”，挂到某个环节试用。
* 不破坏主系统的稳定性。
7.2 必须可视化
要求：
* 至少一套 dashboard 展示：
    * 各域成功率 / 错误率 / 延迟 / 成本
    * Memory Fabric 的规模 / 命中率 / 升级与衰减速度
    * 自编程引擎产生的 Patch 数量 / 通过率 / 回滚率
    * 实验数量 / 通过率 / 影响区间

8. v3 实施路线图（更现实、更研究友好）
在 v2 的 Phase 0–5 上，做一点调整，把“研究”和“工程落地”更清晰地分离。
Phase 0：统一日志 + Trace + 最小骨架（1–2 个月）
* 搭建：
    * kernel/ vs mutable/ vs domain/ 的目录结构
    * 最小 Orchestrator + Memory 接口
* 接入：
    * 统一日志 / Trace（哪怕简单 JSON）
* 目标：
    * 有一个可以跑任务、能看到 Trace 的“AI 中控”。
Phase 1：Memory Fabric v1 + 基础 Reflexion（2–4 个月）
* 实现：
    * Memory CRUD + 向量检索
    * 简单的 STS/MTS/LTS 分层（哪怕先用标志位硬编码规则）
    * 基础 Reflexion（失败一次 → 再想想 → 重试）
* 小目标：
    * Agent 在 Desktop 或 Legal 域能做小任务：先错几次，再通过自反思成功。
Phase 2：Eval Harness + 实验接口 v1（4–6 个月）
* 实现：
    * Offline eval dataset（固定案例集）
    * Online A/B 测试基础框架
    * 简单 create_experiment 接口
* 小目标：
    * 对某个环节（比如 planner 策略）可以：
        * 挂上新版本
        * 用小流量对比新旧效果
Phase 3：Skill Evolution v1（6–9 个月）
* 实现：
    * 从成功 Trace 中抽取简单技能（不要求最优，只要能跑）
    * 在 Sandbox 执行技能测试
    * 替换旧的长流程
* 小目标：
    * 运行几周后，你能在 Skill Library 中看到“不是你亲自写，但系统自动合成”的技能，且确实被调用。
Phase 4：Self-Programming Engine v1（9–14 个月）
* 实现：
    * 针对单一模块（例如 Desktop 的一个工具）做 patch 生成 + 测试。
    * Patch 必须经过：
        * 单元测试
        * 静态分析
        * 基本回归
    * 人工 Gate 才允许部署到 Staging / Shadow。
* 小目标：
    * 在 Desktop 域，允许系统周期性自动改一小段工具代码，并在数轮后指标有稳定改善。
Phase 5：Research Layer & Meta-Optimization v1（14–18+ 个月）
* 实现：
    * System-wide 实验管理（对不同模块、不同策略做试验）
    * 初步资源调度：
        * 哪些模块值得优先优化
        * 哪些实验组“产能高”
* 小目标：
    * 系统能回答你：
        * “最近哪几类自改实验最有收益？”
        * “哪些模块值得继续投入优化？”

9. v3 面临的关键挑战（你刚才问的“需要做哪些挑战”）
从最新论文 + 你蓝皮书的结合来看，真正的“难点清单”可以总结为：
1. 持续记忆 vs 模型上下文的矛盾
    * 如何在有限上下文下，稳定、长期保留关键记忆？
    * 如何决定什么升到 LTS，什么可以遗忘？
    * 如何防止知识碎片化 / 记忆库膨胀？
2. 自编程的可靠性与边界
    * 如何确保自动生成的代码不会破坏现有系统？
    * 测试覆盖率不足怎么补？由谁补？
    * 如何定义“这个 patch 值得上线”的量化标准？
3. 自演化 vs 安全内核之间的张力
    * 你希望它“越来越自主”，但工业级系统要求“可控、安全、可追责”。
    * 怎么在“让它探索”和“及时刹车”之间找到平衡点？
4. 多领域的知识迁移与隔离
    * Desktop 的经验什么时候能迁移到 Legal？
    * Trading 域如何最大化利用别的域的经验，又避免风险扩散？
5. 评测体系的设计难度
    * 对 Trading 来说：收益、回撤、风险暴露如何综合评估？
    * 对 Legal 来说：正确性、合规性、用户满意度如何量化？
    * 对 Desktop 来说：成功率 / 时长 / 错误代价如何统一到一个“自改评分”上？
6. 人类监督的“带宽”与“接口”
    * 不能让你每天手动审批几十个 patch，那太重。
    * 需要设计“关键少量决策点”和“高信息密度的报告格式”。
7. 研究与生产共存
    * 一边要作为“研究平台”尝试新方法，
    * 一边要作为“生产系统”为你真的干活。
    * 如何防止研究实验干扰生产？
    * 如何把研究成果稳定迁移到生产？
