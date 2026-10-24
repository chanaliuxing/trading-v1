# Project Quentin v3.5 用户指南

**版本**: 3.5
**日期**: 2025-12-04
**状态**: 生产就绪 (需谨慎配置)

---

## 目录

1. [快速开始](#快速开始)
2. [核心概念](#核心概念)
3. [配置指南](#配置指南)
4. [使用场景](#使用场景)
5. [测试验证](#测试验证)
6. [故障排查](#故障排查)
7. [安全最佳实践](#安全最佳实践)

---

## 快速开始

### 前置条件

```bash
# 1. 确认环境
python --version  # 需要 Python 3.10+
docker ps         # 确认 ClickHouse, Kafka, Redis 运行中

# 2. 激活虚拟环境
source venv/bin/activate

# 3. 验证依赖
pip list | grep -E "sentence-transformers|chromadb|faiss"
```

### 30 秒快速测试

```bash
# 运行核心功能测试
PYTHONPATH=. venv/bin/pytest tests/unit/evolution/test_governance_v35.py -v

# 预期输出: 9/9 PASSED
```

---

## 核心概念

### 1. 自主演化 (Autonomy)

系统可以根据历史经验自动生成代码补丁和策略调整。

**关键开关**: `config.yml` → `features.v3_autonomy.enable_policy_self_application`
- `false` (默认): 安全模式 - 仅建议，不执行
- `true`: 自主模式 - 根据 governance 规则自动执行

### 2. 安全门控 (Safety Guardrails)

7 层防御确保系统不失控:

| 门控层 | 功能 | 配置位置 |
|--------|------|----------|
| **Gate 1**: Global Kill Switch | 全局紧急停止 | `config.yml` → `enable_policy_self_application` |
| **Gate 2**: Policy Permission | 策略级权限 | `research_policy.json` → `allow_code_evolution` |
| **Gate 3**: Module Whitelist | 模块黑/白名单 | `research_policy.json` → `allowed_modules` |
| **Gate 4**: Test Coverage | 测试覆盖率门槛 (≥85%) | `config.yml` → `auto_merge_min_test_coverage_pct` |
| **Gate 5**: Rate Limiting | 每日补丁限额 | `research_policy.json` → `max_patches_per_day` |
| **Gate 6**: Predictive Immunity | ML 风险预测 | `config.yml` → `immunity.enable_predictive_immunity` |
| **Gate 7**: Policy Protection | 策略文件保护 | `research_policy.json` → `allow_policy_updates` |

### 3. 记忆升级 (Memory Fabric)

使用 BGE-M3 (BAAI/bge-m3) 进行语义记忆检索:
- **向量维度**: 1024-dim
- **上下文长度**: 8192 tokens
- **多语言支持**: 中文、英文等
- **后端**: ChromaDB (持久化) + FAISS (内存)

### 4. 技能合成 (Skill Synthesis)

从历史教训 (Lessons) 自动生成可复用技能 (Skills):

```
Lesson (MTS) → SkillSynthesizer → Pending Skill → Human Review → Skill Registry
```

---

## 配置指南

### 场景 1: 只读模式 (推荐新手)

**目标**: 观察系统建议，不执行任何变更

**配置 `config.yml`**:
```yaml
features:
  v3_autonomy:
    enable_policy_self_application: false  # 🔒 关闭自主演化
    enable_global_model_router: true
    auto_merge_min_test_coverage_pct: 85
```

**配置 `configs/research_policy.json`**:
```json
{
  "governance": {
    "allow_policy_updates": false,   # 🔒 禁止修改策略
    "allow_code_evolution": false    # 🔒 禁止代码演化
  }
}
```

**测试**:
```bash
PYTHONPATH=. venv/bin/python scripts/ai_trader/run_research_cycle.py

# 查看日志 - 应该看到 governance_summary 显示 "blocked"
```

---

### 场景 2: Shadow Mode (测试自主功能)

**目标**: 启用技能生成，但需要人工审核

**配置 `config.yml`**:
```yaml
features:
  v3_autonomy:
    enable_policy_self_application: false  # 🔒 仍然关闭 (生成但不自动应用)
```

**配置 `configs/research_policy.json`**:
```json
{
  "governance": {
    "allow_code_evolution": true,         # ✅ 允许生成代码补丁
    "allowed_modules": [
      "prompts/",
      "docs/",
      "tools/data_quality"
    ],
    "forbidden_modules": [
      "kernel/",
      "risk/",
      "execution/"
    ]
  }
}
```

**测试流程**:
```bash
# 1. 运行 sleep loop (生成技能)
PYTHONPATH=. venv/bin/python scripts/ai_trader/memory/sleep_loop.py

# 2. 查看待审批技能
PYTHONPATH=. venv/bin/python scripts/admin/review_skills.py

# 输出示例:
# --- Pending Skill ---
# ID: risk_control_20251204_030430_59cfbb
# Name: Volatility-Aware Position Sizer
# Category: risk_control
# Type: PURE
# Description: Auto-generated skill from lesson...
# Approve? [y/N]:

# 3. 输入 'y' 批准，'N' 拒绝
```

---

### 场景 3: 渐进式自主 (生产环境)

**目标**: 允许白名单模块自动演化，严格监控

**配置 `config.yml`**:
```yaml
features:
  v3_autonomy:
    enable_policy_self_application: true   # ⚠️  启用自主演化
    auto_merge_min_test_coverage_pct: 90   # 提高门槛
    max_code_patches_per_day: 3            # 限制速率
    max_policy_updates_per_day: 0          # 禁止策略更新
```

**配置 `configs/research_policy.json`**:
```json
{
  "governance": {
    "allow_policy_updates": false,         # 🔒 永久禁止策略自我修改
    "allow_code_evolution": true,
    "allowed_modules": [
      "prompts/tuning/",                   # 仅允许 prompt 调优
      "docs/generated/"                    # 和生成文档
    ],
    "forbidden_modules": [
      "kernel/",
      "risk/",
      "execution/",
      "libs/db/",
      "services/",
      "strategies/core/"
    ],
    "max_patches_per_day": 3,
    "auto_merge_threshold": 0.95           # 非常高的置信度要求
  }
}
```

**监控命令**:
```bash
# 查看 governance 日志
tail -f logs/trading_system.log | grep governance

# 查看每日补丁计数
PYTHONPATH=. venv/bin/python -c "
from libs.evolution.governance import EvolutionGovernance
import yaml
config = yaml.safe_load(open('config.yml'))
policy = yaml.safe_load(open('configs/research_policy.json'))
gov = EvolutionGovernance(config, policy)
print(f'Today patches: {gov._get_daily_patch_count()}')
"
```

---

## 使用场景

### 场景 A: 运行完整研究周期

```bash
# 1. 运行 research cycle
PYTHONPATH=. venv/bin/python scripts/ai_trader/run_research_cycle.py

# 2. 检查生成的 plan
# 日志中查找 "research_plan_enqueued"

# 3. 验证 governance 摘要
# 日志中查找 "governance_summary"
# 应该显示:
# - total_experiments: X
# - allowed_experiments: Y
# - blocked_experiments: Z
```

**预期行为** (默认配置):
- ✅ BGE-M3 加载成功
- ✅ Vector store 初始化
- ✅ Memory context 注入到 LLM prompt
- ✅ Governance 过滤实验
- ⚠️  所有 evolution 实验被阻止 (因为 `enable_policy_self_application: false`)
- ✅ Plan 入队 (但不执行 evolution)

---

### 场景 B: 审批和部署技能

```bash
# 1. 生成技能 (每日自动运行)
PYTHONPATH=. venv/bin/python scripts/ai_trader/memory/sleep_loop.py

# 2. 查看待审批队列
ls -lh runs/memory/skill_approval_queue.jsonl

# 3. 手动审批
PYTHONPATH=. venv/bin/python scripts/admin/review_skills.py

# 4. 查看已批准技能
cat config/skill_registry.json | jq '.skills | keys'

# 5. 使用技能 (在策略中)
# from libs.skills.registry import get_skill
# skill = get_skill("risk_control_20251204_030430_59cfbb")
# result = skill.execute(data)
```

---

### 场景 C: 语义记忆检索

```bash
# 1. 写入记忆
PYTHONPATH=. venv/bin/python -c "
from scripts.ai_trader.memory.memory_fabric import write_memory

write_memory('STS', {
    'msg': 'TSLA 在高波动期间策略表现不佳',
    'symbol': 'TSLA',
    'metric': 'sharpe',
    'value': 0.3
})
print('✅ Memory written to STS')
"

# 2. 语义检索
PYTHONPATH=. venv/bin/python -c "
from scripts.ai_trader.research.memory_client import fetch_relevant_memories

results = fetch_relevant_memories('特斯拉波动', top_k=5)
for i, mem in enumerate(results, 1):
    print(f'{i}. Score: {mem.get(\"score\", 0):.3f}')
    print(f'   Text: {mem.get(\"text\", \"\")[:100]}...')
"
```

**预期输出**:
```
1. Score: 0.874
   Text: {"msg": "TSLA 在高波动期间策略表现不佳", "symbol": "TSLA", ...}
```

---

### 场景 D: 测试 Governance 门控

```bash
# 运行 governance 单元测试
PYTHONPATH=. venv/bin/pytest tests/unit/evolution/test_governance_v35.py -v

# 预期输出:
# ✅ test_gate_1_global_kill_switch              PASSED
# ✅ test_gate_2_policy_level_permission         PASSED
# ✅ test_gate_3_forbidden_modules               PASSED
# ✅ test_gate_3_allowed_modules_whitelist       PASSED
# ✅ test_gate_4_test_coverage_gate              PASSED
# ✅ test_gate_5_daily_rate_limits               PASSED
# ✅ test_gate_7_policy_update_restrictions      PASSED
# ✅ test_all_gates_pass_scenario                PASSED
# ✅ test_governance_summary                     PASSED

# 测试特定场景
PYTHONPATH=. venv/bin/python -c "
from libs.evolution.governance import EvolutionGovernance
import yaml

config = yaml.safe_load(open('config.yml'))
policy = yaml.safe_load(open('configs/research_policy.json'))
gov = EvolutionGovernance(config, policy)

# 测试: 尝试修改 kernel/ (应该被阻止)
decision = gov.check_code_evolution_allowed(
    target_module='kernel/core.py',
    experiment_type='code_patch'
)

print(f'Allowed: {decision.allowed}')
print(f'Reason: {decision.reason}')
print(f'Gate: {decision.gate_triggered}')
"

# 预期输出:
# Allowed: False
# Reason: 🔒 Autonomy disabled by config
# Gate: global_kill_switch
```

---

## 测试验证

### 完整测试套件

```bash
# 1. 单元测试 (快速)
PYTHONPATH=. venv/bin/pytest tests/unit/evolution/ -v        # Governance (9 tests)
PYTHONPATH=. venv/bin/pytest tests/unit/memory/ -v           # BGE-M3 Memory (7 tests)
PYTHONPATH=. venv/bin/pytest tests/integration/test_skill_synthesis_e2e.py -v  # Skills (18 tests)

# 2. 集成测试 (5-15 分钟)
PYTHONPATH=. venv/bin/pytest tests/integration/test_p5_e2e_memory_evolution.py -v  # E2E (9 tests)

# 3. 快速冒烟测试 (按顺序执行)
# Test 1: Research Cycle
PYTHONPATH=. venv/bin/python scripts/ai_trader/run_research_cycle.py

# Test 2: Skill Synthesis
PYTHONPATH=. venv/bin/python scripts/ai_trader/memory/sleep_loop.py

# Test 3: Memory Vectorization
PYTHONPATH=. venv/bin/python -c "
from scripts.ai_trader.memory.memory_fabric import write_memory
from scripts.ai_trader.research.memory_client import fetch_relevant_memories
write_memory('STS', {'msg': 'test semantic memory'})
res = fetch_relevant_memories('semantic', top_k=3)
print(f'✅ Fetched {len(res)} memories')
"

# Test 4: Scheduler Health
PYTHONPATH=. venv/bin/python tools/nightly_oof_flow.py --symbols TSLA,NVDA --window-days 7 --quick
```

### 测试结果验证

**成功标准**:
- ✅ 所有单元测试通过 (34/34)
- ✅ 所有集成测试通过 (9/9)
- ✅ 无 `Logger._log() got an unexpected keyword argument 'error'` 错误
- ✅ `research_policy.json` 未被修改 (除非显式允许)
- ✅ Governance summary 正确生成

**查看测试报告**:
```bash
cat docs/V3_5_QUICK_TEST_SUMMARY.md
```

---

## 故障排查

### 问题 1: BGE-M3 加载失败

**症状**:
```
ModuleNotFoundError: No module named 'sentence_transformers'
```

**解决**:
```bash
pip install sentence-transformers
pip install chromadb faiss-cpu
```

---

### 问题 2: Vector Store 初始化失败

**症状**:
```
vector_store_init_failed: No module named 'chromadb'
```

**解决**:
```bash
# 1. 安装依赖
pip install chromadb

# 2. 创建目录
mkdir -p runs/memory/vector_store

# 3. 验证
PYTHONPATH=. venv/bin/python -c "
from libs.memory.vector_store import VectorStore
store = VectorStore(backend='chroma')
print('✅ ChromaDB initialized')
"
```

---

### 问题 3: LLM 响应解析失败

**症状**:
```
telemetry.summary: "llm_unavailable_or_parse_error"
```

**原因**:
1. LLM 返回包含 markdown 格式
2. LLM 未返回有效 JSON

**解决**:
```bash
# 1. 检查 LLM 配置
cat config.yml | grep -A 5 model_router

# 2. 测试 LLM 连接
PYTHONPATH=. venv/bin/python -c "
from scripts.ai_trader.brain.provider_pool import ProviderPool
pool = ProviderPool()
result = pool.call_with_fallback('Test prompt', operation='test')
print(f'✅ LLM response: {result.raw[:100]}...')
"

# 3. 如果仍然失败，检查 prompt (已在 v3.5 中强化):
# RESEARCH_AGENT_PROMPT 包含:
# "CRITICAL: Output MUST be valid JSON ONLY. No explanations, no markdown, no code blocks."
```

---

### 问题 4: Skill Approval Queue 为空

**症状**:
```bash
$ PYTHONPATH=. venv/bin/python scripts/admin/review_skills.py
No pending skills found.
```

**原因**:
1. 数据路径不匹配 (已在 v3.5 中修复)
2. 没有生成技能 (Lessons 不足或不符合条件)

**解决**:
```bash
# 1. 检查文件是否存在
ls -lh runs/memory/skill_approval_queue.jsonl

# 2. 如果文件存在但为空
cat runs/memory/skill_approval_queue.jsonl | wc -l

# 3. 检查 Lessons
PYTHONPATH=. venv/bin/python -c "
from scripts.ai_trader.memory.memory_fabric import list_entries
lessons = [e for e in list_entries() if e.memory_tier == 'MTS']
print(f'Total lessons in MTS: {len(lessons)}')
"

# 4. 手动运行 skill synthesis
PYTHONPATH=. venv/bin/python scripts/ai_trader/memory/sleep_loop.py
```

---

### 问题 5: Governance 意外阻止合法操作

**症状**:
```
governance_summary.blocked_experiments: [...]
blocked_reason: "🔒 Autonomy disabled by config"
```

**诊断**:
```bash
PYTHONPATH=. venv/bin/python -c "
from libs.evolution.governance import EvolutionGovernance
import yaml

config = yaml.safe_load(open('config.yml'))
policy = yaml.safe_load(open('configs/research_policy.json'))
gov = EvolutionGovernance(config, policy)

# 测试你的操作
decision = gov.check_code_evolution_allowed(
    target_module='YOUR_MODULE',
    experiment_type='code_patch'
)

print(f'Gate triggered: {decision.gate_triggered}')
print(f'Reason: {decision.reason}')

# 打印配置状态
print(f'\\nGlobal kill switch: {config[\"features\"][\"v3_autonomy\"][\"enable_policy_self_application\"]}')
print(f'Policy allows evolution: {policy[\"governance\"][\"allow_code_evolution\"]}')
print(f'Module in whitelist: {any(\"YOUR_MODULE\".startswith(m) for m in policy[\"governance\"][\"allowed_modules\"])}')
"
```

**修复**:
根据诊断结果修改 `config.yml` 或 `research_policy.json`

---

## 安全最佳实践

### ✅ DO (推荐)

1. **渐进式启用**:
   ```
   Phase 1 (1周): 只读模式 → 观察建议
   Phase 2 (2周): Shadow模式 → 生成但人工审核
   Phase 3 (1月): 白名单自主 → 限定模块自动演化
   ```

2. **分层防御**:
   - 永久禁止修改核心模块 (`kernel/`, `risk/`, `execution/`)
   - 永久禁止策略自我修改 (`allow_policy_updates: false`)
   - 设置严格的测试覆盖率要求 (≥90%)

3. **监控和告警**:
   ```bash
   # 每日检查
   - Governance logs: blocked_experiments 数量
   - Patch count: 是否超过限额
   - Policy file MD5: 是否被意外修改
   ```

4. **定期审计**:
   ```bash
   # 每周审查
   git log --since="1 week ago" --all -- 'libs/skills/generated/*'
   git log --since="1 week ago" --all -- 'configs/research_policy.json'
   ```

---

### ❌ DON'T (禁止)

1. **❌ 不要在生产环境首次启用 `enable_policy_self_application: true`**
   - 先在测试环境运行至少 2 周

2. **❌ 不要允许修改核心基础设施**:
   ```json
   // ❌ 错误配置
   {
     "allowed_modules": ["*"]  // 允许一切
   }

   // ✅ 正确配置
   {
     "allowed_modules": ["prompts/", "docs/"],
     "forbidden_modules": ["kernel/", "risk/", "execution/", "libs/db/"]
   }
   ```

3. **❌ 不要关闭所有 governance gates**:
   ```yaml
   # ❌ 危险配置
   features:
     v3_autonomy:
       enable_policy_self_application: true
       auto_merge_min_test_coverage_pct: 0  # 无门槛
       max_code_patches_per_day: 999        # 无限制
   ```

4. **❌ 不要忽略 blocked_experiments**:
   - 如果大量实验被阻止，检查是否配置过于严格
   - 如果没有实验被阻止，检查是否配置过于宽松

---

## 紧急停止流程

如果系统行为异常:

```bash
# 1. 立即关闭 Global Kill Switch
# 编辑 config.yml
features:
  v3_autonomy:
    enable_policy_self_application: false  # 🔒 紧急停止

# 2. 回滚最近的自动提交
git log --all --grep="Generated with Claude Code" --since="1 day ago"
git revert <commit-hash>

# 3. 检查 policy 文件
md5 configs/research_policy.json
# 与备份比对，如有变化则恢复备份

# 4. 清空待审批队列 (可选)
> runs/memory/skill_approval_queue.jsonl

# 5. 重启服务
make down && make up
```

---

## 参考文档

- **完整实现文档**: `docs/V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md`
- **测试结果**: `docs/V3_5_QUICK_TEST_SUMMARY.md`
- **架构参考**: `CLAUDE.md` (Section: "策略开发指南")

---

## 附录: 配置模板

### 模板 1: 保守配置 (推荐生产初期)

**`config.yml`**:
```yaml
features:
  v3_autonomy:
    enable_policy_self_application: false
    enable_global_model_router: true
    auto_merge_min_test_coverage_pct: 95
    max_policy_updates_per_day: 0
    max_code_patches_per_day: 1

memory:
  embedding_provider: "local_bge"
  vector_db_path: "./runs/memory/vector_store"
  local_model:
    name: "BAAI/bge-m3"
    device: "cpu"

immunity:
  enable_predictive_immunity: true
  lookback_window_size: 100
  thresholds:
    freeze_exploration: 0.7
    freeze_evolution: 0.8
    freeze_all: 0.9
```

**`configs/research_policy.json`**:
```json
{
  "version": "3.0.0",
  "governance": {
    "allow_policy_updates": false,
    "allow_code_evolution": true,
    "auto_merge_threshold": 0.95,
    "allowed_modules": [
      "prompts/tuning/",
      "docs/generated/"
    ],
    "forbidden_modules": [
      "kernel/",
      "risk/",
      "execution/",
      "libs/db/",
      "services/",
      "strategies/core/",
      "infra/"
    ],
    "max_patches_per_day": 1
  }
}
```

---

### 模板 2: 激进配置 (仅测试环境)

**`config.yml`**:
```yaml
features:
  v3_autonomy:
    enable_policy_self_application: true
    auto_merge_min_test_coverage_pct: 85
    max_code_patches_per_day: 5
```

**`configs/research_policy.json`**:
```json
{
  "governance": {
    "allow_policy_updates": false,
    "allow_code_evolution": true,
    "allowed_modules": [
      "prompts/",
      "docs/",
      "tools/",
      "libs/skills/generated/"
    ],
    "forbidden_modules": [
      "kernel/",
      "risk/",
      "execution/"
    ],
    "max_patches_per_day": 5
  }
}
```

---

**文档版本**: 1.0
**最后更新**: 2025-12-04
**维护者**: Trading Team

如有问题，请查看 `docs/V3_5_QUICK_TEST_SUMMARY.md` 或联系团队。
