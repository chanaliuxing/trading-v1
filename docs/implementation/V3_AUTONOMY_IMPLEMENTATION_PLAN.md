### **Project Quentin v3 - 自主功能落地技术方案 (v1.0)**

**目标**：以安全、可控的方式，为系统引入最后的“自主”能力，所有新功能均由 `config.yml` 中的开关控制。

---

#### **第 1 步：在 `config.yml` 中建立“v3 自治总开关”**

我们将在 `config.yml` 文件末尾添加一个集中的 `features` 控制区域，这是所有后续修改的基础。

**`config.yml` 新增内容：**
```yaml
features:
  v3_autonomy:
    # 开关 1: 是否允许 AI Agent 自动应用自己提出的策略(Policy)变更
    # true: AI 将尝试自动修改自身的参数（如预算、探索率）。
    # false: AI 的策略修改建议只会被记录，需人工干预。
    enable_policy_self_application: false

    # 开关 2: 是否启用全局统一的模型路由服务
    # true: 所有模块将通过全局路由调用 LLM，方便统一管理。
    # false: 各模块使用自己内部定义的本地模型。
    enable_global_model_router: false

    # 开关 3: 自动合并代码的最低测试覆盖率要求
    # 仅当被修改文件的测试覆盖率高于此值时，AI 的代码修改才会被自动合并。
    # 设置为 0 表示禁用此检查。
    auto_merge_min_test_coverage_pct: 90

# 新增一个配套的全局模型路由配置
model_router:
  default_capability: research # 默认使用的模型池
  pools:
    research: # 用于研究和决策的模型池
      - name: ollama
        model: my-deepseek-uncensored:latest
        priority: 1
    summary: # 用于总结和归纳的模型池
      - name: ollama
        model: qwen2.5vl:latest
        priority: 1
    code_gen: # 用于代码生成的模型池
      - name: ollama
        model: fast-local-1:latest
        priority: 1
```

---

#### **第 2 步：实现“策略自进化”开关**

**目标**：解决“AI 只能提建议，无法自己改策略”的问题。

1.  **修改文件**: `scripts/ai_trader/research/agent.py`
2.  **实现逻辑**:
    *   在 `ResearchAgent` 的 `__init__` 方法中，加载 `config.yml` 中的 `enable_policy_self_application` 开关。
    *   在 `_execute_plan` 方法中，定位到 `self._log_policy_patches(...)` 这行代码。
    *   **添加 `if` 逻辑**：
        *   **如果开关为 `true`**：
            1.  调用一个新增的 `_apply_policy_patch` 辅助函数。
            2.  此函数将读取 `scripts/ai_trader/research/policy.py`，以安全的方式（例如，使用 `ast` 模块或安全的 `exec`）应用补丁中的参数变更，然后将更新后的内容写回文件。
            3.  同时记录一条表明“策略已自动应用”的日志。
        *   **如果开关为 `false`**：保持现有逻辑不变，只记录日志。

---

#### **第 3 步：实现“全局模型路由”开关**

**目标**：解决“模型配置分散，难以统一管理”的问题。

1.  **新建文件**: `libs/llm/global_model_router.py`
    *   **实现 `GlobalModelRouter` 类**：
        *   此类在初始化时，将加载 `config.yml` 中新增的 `model_router` 配置段。
        *   提供一个核心方法 `get_pool(capability: str) -> ProviderPool`，它能根据请求的能力（如 'research', 'summary'）返回一个配置好的 `ProviderPool` 实例。

2.  **修改文件**: `scripts/ai_trader/research/agent.py`
    *   **改造 `_build_default_pool` 方法**：
        *   首先读取 `enable_global_model_router` 开关。
        *   **如果开关为 `true`**：调用 `GlobalModelRouter` 来获取模型池，取代原有的硬编码逻辑。
        *   **如果开关为 `false`**：保留原有的硬编码逻辑，作为旧版行为的兼容。

---

#### **第 4 步：为“代码自动合并”增加“测试覆盖率”门禁**

**目标**：解决“测试不全的代码被自动合并”的风险。

1.  **修改文件**: `scripts/ai_trader/evolvers/code_evolver_git.py`
2.  **实现逻辑**:
    *   在 `main` 函数中，加载 `auto_merge_min_test_coverage_pct` 配置值。
    *   在调用 `gs.auto_merge` 之前，**增加一个前置检查**。
    *   **新增 `_check_test_coverage(skill_path, min_coverage)` 辅助函数**：
        *   此函数会查找并解析项目根目录下的 `coverage.json` 文件。**（前置要求：CI/CD 流水线必须在测试阶段生成此文件）**
        *   从报告中提取 `skill_path` 对应文件的覆盖率数据。
        *   如果覆盖率 `>`=` min_coverage` 或 `min_coverage` 为 `0`，则返回 `True`。
    *   只有当上述覆盖率检查通过时，才会执行 `auto_merge`。否则，记录一条“因测试覆盖率不足而跳过自动合并”的日志。
