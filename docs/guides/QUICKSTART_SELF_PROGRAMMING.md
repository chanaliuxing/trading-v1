# 🚀 自编程系统 - 5 分钟快速开始

## 📋 前提条件

```bash
# 1. 确保你在 trading 目录
cd .

# 2. Python 环境已激活
# (你的环境应该已经配置好)

# 3. 验证测试通过
pytest tests/test_file_patcher.py tests/test_demo.py -v
# 预期: 15 passed ✅
```

---

## 🎯 场景 1: 第一次使用（Dry Run）

```bash
# 不调用 LLM，不修改文件，只看流程
python scripts/run_self_programming.py \
    --task "Add type hints to demo.py functions" \
    --files demo.py \
    --dry-run
```

**预期输出:**
```
🔍 DRY RUN MODE: No actual changes will be made
============================================================
SELF-PROGRAMMING RESULT
============================================================
📋 Task: manual-XXXXXXXX
   Description: Add type hints to demo.py functions
...
✅ Pipeline completed
```

---

## 🎯 场景 2: 真实使用（需要本地 LLM）

### 前提: Ollama + qwen2.5 运行中

```bash
# 1. 确保 Ollama 在运行
ollama list
# 应该看到 qwen2.5:7b-instruct 或 deepseek-r1:1.5b

# 2. 运行自编程（带 LLM）
python scripts/run_self_programming.py \
    --task "Add comprehensive docstrings to calculate_sum function in demo.py" \
    --files demo.py \
    --use-llm \
    --use-sandbox  # 使用 Git 沙盒分支
```

**发生了什么:**
1. 创建 `ai-dev/task-xxx` Git 分支
2. 调用 qwen2.5 生成代码修改
3. FilePatcher 应用修改到 demo.py
4. 运行 pytest 验证
5. ReviewerAgent 检查测试结果
6. 如果测试通过 → commit；失败 → rollback

---

## 🎯 场景 3: 查看示例

```bash
# 运行所有预设示例
python scripts/example_self_programming_usage.py
```

**你会看到:**
- ✅ Example 1: 简单重构（dry run）
- ✅ Example 2: LLM 代码生成
- ✅ Example 3: 多文件任务
- ✅ Example 4: Blueprint 集成模式
- ✅ Example 5: 安全特性演示

---

## 🎯 场景 4: 在代码中使用

```python
#!/usr/bin/env python3
"""你的自动化脚本"""
from meta_agent.self_programming.engine import SelfProgrammingEngine
from meta_agent.self_programming.integration import create_gap_from_todo

# 1. 创建任务
gap = create_gap_from_todo(
    description="Add logging to all critical functions",
    files=["scripts/ai_trader/monitoring_dashboard.py"],
    category="improvement",
    severity="low"
)

# 2. 转换为编程任务
from meta_agent.self_programming.integration import blueprint_gap_to_programming_task
task = blueprint_gap_to_programming_task(gap)

if not task:
    print("❌ Task rejected: files not in whitelist")
    exit(1)

# 3. 运行自编程
engine = SelfProgrammingEngine(use_llm=True)
result = engine.run(task, use_sandbox=True)

# 4. 检查结果
if result.review.status == "approve":
    print(f"✅ Success! Modified {len(result.patch_set.files)} files")
    print(f"   Git branch: ai-dev/...")
    print(f"   Tests: {result.test_report.overall_status}")
else:
    print(f"❌ Rejected: {result.review.summary}")
    for issue in result.review.issues:
        print(f"   - {issue['description']}")
```

---

## 🔧 常用命令速查

### 基础命令

```bash
# 显示帮助
python scripts/run_self_programming.py --help

# Dry-run（最安全）
python scripts/run_self_programming.py \
    --task "你的任务" \
    --files file1.py,file2.py \
    --dry-run

# 使用 LLM + Git 沙盒（推荐）
python scripts/run_self_programming.py \
    --task "你的任务" \
    --files file1.py \
    --use-llm \
    --use-sandbox

# 强制执行（绕过白名单，慎用）
python scripts/run_self_programming.py \
    --task "你的任务" \
    --files some_file.py \
    --use-llm \
    --force

# 保存结果到 JSON
python scripts/run_self_programming.py \
    --task "你的任务" \
    --files file.py \
    --use-llm \
    --output-json result.json
```

### 安全选项

```bash
# 设置风险等级
--risk-level low|medium|high

# 限制最大文件数
--max-files 3

# 强制白名单检查（默认）
--enforce-whitelist

# 跳过测试（不推荐）
--skip-tests
```

---

## 📊 验证安装

```bash
# 1. 运行所有测试
pytest tests/test_file_patcher.py tests/test_demo.py -v

# 2. E2E 测试
python test_self_programming_e2e.py

# 3. 示例
python scripts/example_self_programming_usage.py

# 预期: 所有测试通过 ✅
```

---

## 🔍 故障排查

### 问题 1: "Files not in whitelist"

```bash
# 方案 A: 添加文件到白名单
# 编辑: meta_agent/self_programming/integration/blueprint_adapter.py
# 在 SAFE_FILES_WHITELIST 中添加你的文件模式

# 方案 B: 使用 --force（仅测试用）
python scripts/run_self_programming.py ... --force
```

### 问题 2: "LLM call failed"

```bash
# 检查 Ollama 是否运行
ollama list

# 手动测试 LLM
ollama run qwen2.5:7b-instruct "Hello"

# 查看 LocalLLMTool 配置
# meta_agent/self_programming/tools/local_llm.py:32
```

### 问题 3: "Tests timeout"

```bash
# 增加超时时间
# 编辑 meta_agent/self_programming/agents/tester.py:66
# 将 timeout=60 改为 timeout=120
```

---

## 📚 进一步学习

| 资源 | 路径 |
|------|------|
| 完整文档 | `meta_agent/self_programming/README.md` |
| 完成报告 | `SELF_PROGRAMMING_COMPLETE.md` |
| API 参考 | `meta_agent/self_programming/README.md#api-reference` |
| 示例代码 | `scripts/example_self_programming_usage.py` |

---

## 🎯 下一步

1. **熟悉 Dry-run 模式**
   ```bash
   python scripts/run_self_programming.py --task "..." --files ... --dry-run
   ```

2. **尝试小任务**（文档/注释）
   ```bash
   python scripts/run_self_programming.py \
       --task "Add docstring to foo() function" \
       --files demo.py \
       --use-llm \
       --use-sandbox
   ```

3. **集成到你的工作流**
   - 从 Blueprint gaps 创建任务
   - 集成到 Sleep Loop
   - 添加到 CI/CD

4. **扩展白名单**
   - 添加你的安全文件
   - 定义风险策略
   - 配置自动化规则

---

## ⚡ 一键测试

```bash
# 完整测试流程（约 2 分钟）
pytest tests/test_file_patcher.py tests/test_demo.py -v && \
python test_self_programming_e2e.py && \
python scripts/example_self_programming_usage.py && \
echo "✅ 所有测试通过！系统就绪！"
```

---

**🚀 开始使用自编程系统，让 AI 帮你写代码！**
