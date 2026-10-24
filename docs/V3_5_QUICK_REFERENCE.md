# V3.5 快速参考卡片 (5分钟速查)

## 🚦 状态检查 (30秒)

```bash
# 检查当前配置状态
PYTHONPATH=. venv/bin/python -c "
import yaml
config = yaml.safe_load(open('config.yml'))
policy = yaml.safe_load(open('configs/research_policy.json'))

print('🔒 Global Kill Switch:', config['features']['v3_autonomy']['enable_policy_self_application'])
print('📋 Policy Allows Evolution:', policy['governance']['allow_code_evolution'])
print('🚫 Forbidden Modules:', len(policy['governance']['forbidden_modules']))
print('✅ Allowed Modules:', len(policy['governance']['allowed_modules']))
print('📊 Test Coverage Required:', config['features']['v3_autonomy']['auto_merge_min_test_coverage_pct'], '%')
print('⏱️  Daily Patch Limit:', policy['governance']['max_patches_per_day'])
"
```

**预期输出 (安全默认)**:
```
🔒 Global Kill Switch: False
📋 Policy Allows Evolution: True
🚫 Forbidden Modules: 9
✅ Allowed Modules: 6
📊 Test Coverage Required: 85 %
⏱️  Daily Patch Limit: 3
```

---

## 🎯 核心命令

| 任务 | 命令 | 时长 |
|------|------|------|
| **测试 Governance** | `PYTHONPATH=. venv/bin/pytest tests/unit/evolution/test_governance_v35.py -v` | 5秒 |
| **运行研究周期** | `PYTHONPATH=. venv/bin/python scripts/ai_trader/run_research_cycle.py` | 2分钟 |
| **生成技能** | `PYTHONPATH=. venv/bin/python scripts/ai_trader/memory/sleep_loop.py` | 30秒 |
| **审批技能** | `PYTHONPATH=. venv/bin/python scripts/admin/review_skills.py` | 交互式 |
| **检查待审批** | `wc -l runs/memory/skill_approval_queue.jsonl` | 即时 |
| **查看已批准技能** | `cat config/skill_registry.json \| jq '.skills \| keys'` | 即时 |

---

## 📋 配置速查表

### 关键开关位置

| 功能 | 文件 | 路径 | 默认值 |
|------|------|------|--------|
| **全局自主开关** | `config.yml` | `features.v3_autonomy.enable_policy_self_application` | `false` ✅ |
| **代码演化权限** | `research_policy.json` | `governance.allow_code_evolution` | `true` |
| **策略自我修改** | `research_policy.json` | `governance.allow_policy_updates` | `false` ✅ |
| **测试覆盖率门槛** | `config.yml` | `features.v3_autonomy.auto_merge_min_test_coverage_pct` | `85` |
| **每日补丁限额** | `research_policy.json` | `governance.max_patches_per_day` | `3` |

### 安全配置组合

| 模式 | `enable_policy_self_application` | `allow_code_evolution` | `allow_policy_updates` | 说明 |
|------|----------------------------------|------------------------|------------------------|------|
| **只读** | `false` | `false` | `false` | 仅观察，不执行 |
| **Shadow** | `false` | `true` | `false` | 生成但需审批 |
| **渐进式自主** | `true` | `true` | `false` | 白名单模块自动演化 |
| **⚠️ 危险** | `true` | `true` | `true` | 允许策略自我修改 |

---

## 🔍 快速诊断

### 问题: 所有实验被阻止

```bash
# 检查 kill switch
grep enable_policy_self_application config.yml

# 如果是 false，改为 true (仅测试环境！)
```

---

### 问题: BGE-M3 加载失败

```bash
# 安装依赖
pip install sentence-transformers chromadb faiss-cpu

# 验证
PYTHONPATH=. venv/bin/python -c "
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('BAAI/bge-m3')
print('✅ BGE-M3 loaded')
"
```

---

### 问题: 无待审批技能

```bash
# 1. 检查文件
ls -lh runs/memory/skill_approval_queue.jsonl

# 2. 检查 Lessons 数量
PYTHONPATH=. venv/bin/python -c "
from scripts.ai_trader.memory.memory_fabric import list_entries
lessons = [e for e in list_entries() if e.memory_tier == 'MTS']
print(f'MTS Lessons: {len(lessons)}')
"

# 3. 如果 Lessons 少于 10 个，需要积累更多经验
```

---

### 问题: LLM 解析失败

```bash
# 查看最近的 research plan
tail -100 logs/trading_system.log | grep -A 20 "research_plan_enqueued"

# 检查 telemetry.summary
# 如果是 "llm_unavailable_or_parse_error"，说明 LLM 未返回有效 JSON
```

---

## 🧪 测试矩阵

| 测试类型 | 命令 | 预期结果 | 时长 |
|---------|------|----------|------|
| **单元 - Governance** | `pytest tests/unit/evolution/test_governance_v35.py -v` | 9/9 PASSED | 5秒 |
| **单元 - BGE-M3** | `pytest tests/unit/memory/test_vector_store_bge_m3.py -v` | 7/7 PASSED | 10秒 |
| **单元 - Skills** | `pytest tests/integration/test_skill_synthesis_e2e.py -v` | 18/18 PASSED | 30秒 |
| **E2E - Memory Evolution** | `pytest tests/integration/test_p5_e2e_memory_evolution.py -v` | 9/9 PASSED | 2-3分钟 |

---

## 📊 监控指标

### 每日检查清单

```bash
# 1. Governance 状态
grep "governance_summary" logs/trading_system.log | tail -1

# 2. 今日补丁计数
PYTHONPATH=. venv/bin/python -c "
from libs.evolution.governance import EvolutionGovernance
import yaml
config = yaml.safe_load(open('config.yml'))
policy = yaml.safe_load(open('configs/research_policy.json'))
gov = EvolutionGovernance(config, policy)
count = gov._get_daily_patch_count()
limit = policy['governance']['max_patches_per_day']
print(f'Patches: {count}/{limit}')
if count >= limit:
    print('⚠️  Daily limit reached!')
"

# 3. Policy 文件完整性
md5 configs/research_policy.json
# 与基线对比: 81e10a8c90a4b4d87fa8aebb8a6d1faa

# 4. Vector store 健康
ls -lh runs/memory/vector_store/chroma.sqlite3

# 5. Skill 队列长度
wc -l runs/memory/skill_approval_queue.jsonl
```

---

## 🚨 紧急操作

### 立即停止自主演化

```bash
# 方法 1: 修改配置文件
sed -i '' 's/enable_policy_self_application: true/enable_policy_self_application: false/' config.yml

# 方法 2: 直接编辑
# 编辑 config.yml，设置 enable_policy_self_application: false

# 验证
grep enable_policy_self_application config.yml
```

---

### 回滚最近的自动提交

```bash
# 1. 查找自动提交
git log --all --grep="Generated with Claude Code" --since="1 day ago" --oneline

# 2. 回滚
git revert <commit-hash>

# 3. 验证
git log -1
```

---

### 恢复 Policy 文件

```bash
# 1. 检查当前 MD5
md5 configs/research_policy.json

# 2. 从 Git 恢复
git checkout HEAD -- configs/research_policy.json

# 3. 验证
md5 configs/research_policy.json
# 应该是: 81e10a8c90a4b4d87fa8aebb8a6d1faa
```

---

## 💡 速记规则

### ✅ 永远安全

- `enable_policy_self_application: false` (全局关闭)
- `allow_policy_updates: false` (禁止策略自我修改)
- `forbidden_modules: [kernel/, risk/, execution/]` (保护核心)
- `auto_merge_min_test_coverage_pct: 90+` (高门槛)

### ⚠️ 谨慎使用

- `enable_policy_self_application: true` (仅测试环境)
- `allowed_modules: [prompts/, docs/]` (限定白名单)
- `max_patches_per_day: 1-3` (严格限流)

### ❌ 禁止生产

- `allow_policy_updates: true` (允许策略自我修改)
- `allowed_modules: ["*"]` (允许修改所有模块)
- `max_patches_per_day: 999` (无限制)
- `auto_merge_min_test_coverage_pct: 0` (无门槛)

---

## 📞 获取帮助

| 文档 | 用途 |
|------|------|
| **`V3_5_USER_GUIDE.md`** | 完整使用指南 (本文档) |
| **`V3_5_QUICK_TEST_SUMMARY.md`** | 测试结果和发现 |
| **`V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md`** | 完整实现细节 |
| **`CLAUDE.md`** | 项目总体规则 |

---

**最后更新**: 2025-12-04
**适用版本**: v3.5
